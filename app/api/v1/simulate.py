from __future__ import annotations
import asyncio
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.router import CurrentMerchant, DBSession, RedisClient
from app.models.transaction import Transaction
from app.schemas.transaction import TransactionResponse
from app.services.fraud_engine import FraudEngine
from app.services.idempotency import IdempotencyService
from app.services.lock_manager import LockManager
from app.services.transaction_engine import TransactionEngine, TransactionError
from app.schemas.transaction import TransactionCreate
from app.models.transaction import PaymentRail, TransactionType
from app.models.webhook import WebhookEventType
from app.services.webhook_dispatcher import WebhookDispatcher

router = APIRouter(prefix="/simulate", tags=["simulate"])


# ── Request schemas ───────────────────────────────────────

class IdempotencyTestRequest(BaseModel):
    amount: Decimal
    currency: str
    idempotency_key: str
    fire_count: int = 3


class RaceTestRequest(BaseModel):
    amount: Decimal
    currency: str
    concurrent_requests: int = 5


class FraudTestRequest(BaseModel):
    amount: Decimal
    currency: str
    scenario: str = "velocity"


class WebhookTestRequest(BaseModel):
    transaction_id: UUID
    event_type: WebhookEventType
    force_fail: bool = False


class IdempotencyTestResult(BaseModel):
    total_requests: int
    duplicates_detected: int
    unique_processed: int
    idempotency_key: str
    results: list[dict]


class RaceTestResult(BaseModel):
    total_requests: int
    succeeded: int
    failed_lock_contention: int
    failed_insufficient_funds: int
    final_balance: str
    results: list[dict]


class FraudTestResult(BaseModel):
    scenario: str
    fraud_score: int
    flags: dict
    should_block: bool
    should_review: bool
    reasons: list[str]


class WebhookTestResult(BaseModel):
    delivery_id: str
    event_type: str
    status: str
    response_status_code: int | None
    duration_ms: int | None
    error_message: str | None


@router.post("/idempotency", response_model=IdempotencyTestResult)
async def test_idempotency(
    payload: IdempotencyTestRequest,
    current: CurrentMerchant,
    db: DBSession,
    redis: RedisClient,
) -> IdempotencyTestResult:
    """
    Fires the same request multiple times with the same
    idempotency key. Only one should process — the rest
    return the cached result as duplicates.
    """
    engine = TransactionEngine(db, redis)
    results = []
    duplicates = 0
    unique = 0

    for i in range(payload.fire_count):
        try:
            transaction_payload = TransactionCreate(
                amount=payload.amount,
                currency=payload.currency,
                payment_rail=PaymentRail.CARD,
                transaction_type=TransactionType.PAYMENT,
                idempotency_key=payload.idempotency_key,
            )
            result, is_duplicate = await engine.process_payment(
                merchant_id=current.id,
                payload=transaction_payload,
            )
            if is_duplicate:
                duplicates += 1
                results.append({
                    "attempt": i + 1,
                    "is_duplicate": True,
                    "status": "duplicate_detected",
                    "reference": result.get("reference"),
                })
            else:
                unique += 1
                results.append({
                    "attempt": i + 1,
                    "is_duplicate": False,
                    "status": result.get("status"),
                    "reference": result.get("reference"),
                })
        except Exception as e:
            results.append({
                "attempt": i + 1,
                "is_duplicate": False,
                "status": "error",
                "error": str(e),
            })

    return IdempotencyTestResult(
        total_requests=payload.fire_count,
        duplicates_detected=duplicates,
        unique_processed=unique,
        idempotency_key=payload.idempotency_key,
        results=results,
    )


@router.post("/race-condition", response_model=RaceTestResult)
async def test_race_condition(
    payload: RaceTestRequest,
    current: CurrentMerchant,
    db: DBSession,
    redis: RedisClient,
) -> RaceTestResult:
    """
    Fires multiple concurrent requests against the same wallet.
    Demonstrates that distributed locking prevents double-spends.
    Only one should succeed — the rest fail with lock contention
    or insufficient funds.
    """
    engine = TransactionEngine(db, redis)
    results = []
    succeeded = 0
    failed_lock = 0
    failed_funds = 0

    async def single_request(index: int) -> dict:
        try:
            transaction_payload = TransactionCreate(
                amount=payload.amount,
                currency=payload.currency,
                payment_rail=PaymentRail.WALLET,
                transaction_type=TransactionType.PAYMENT,
            )
            result, _ = await engine.process_payment(
                merchant_id=current.id,
                payload=transaction_payload,
            )
            return {
                "request": index + 1,
                "status": "succeeded",
                "reference": result.get("reference"),
            }
        except Exception as e:
            error = str(e).lower()
            if "lock" in error:
                return {"request": index + 1, "status": "lock_contention", "error": str(e)}
            if "insufficient" in error:
                return {"request": index + 1, "status": "insufficient_funds", "error": str(e)}
            return {"request": index + 1, "status": "failed", "error": str(e)}

    # Fire all requests concurrently
    raw_results = await asyncio.gather(
        *[single_request(i) for i in range(payload.concurrent_requests)],
        return_exceptions=False,
    )

    for r in raw_results:
        results.append(r)
        if r["status"] == "succeeded":
            succeeded += 1
        elif r["status"] == "lock_contention":
            failed_lock += 1
        elif r["status"] == "insufficient_funds":
            failed_funds += 1

    # Get final balance
    from app.models.account import Account, AccountType
    balance_result = await db.execute(
        select(Account).where(
            Account.merchant_id == current.id,
            Account.account_type == AccountType.WALLET,
            Account.currency == payload.currency,
        )
    )
    account = balance_result.scalar_one_or_none()
    final_balance = str(account.cached_balance) if account else "unknown"

    return RaceTestResult(
        total_requests=payload.concurrent_requests,
        succeeded=succeeded,
        failed_lock_contention=failed_lock,
        failed_insufficient_funds=failed_funds,
        final_balance=final_balance,
        results=results,
    )


@router.post("/fraud", response_model=FraudTestResult)
async def test_fraud(
    payload: FraudTestRequest,
    current: CurrentMerchant,
    db: DBSession,
    redis: RedisClient,
) -> FraudTestResult:
    """
    Runs the fraud engine against a transaction without
    actually processing it. Shows exactly which rules
    triggered and why.
    """
    fraud_engine = FraudEngine(db, redis)

    # Pre-seed velocity for velocity scenario
    if payload.scenario == "velocity":
        velocity_key = f"fraud:velocity:{current.id}"
        await redis.set(velocity_key, "999", ex=60)

    transaction_payload = TransactionCreate(
        amount=payload.amount,
        currency=payload.currency,
        payment_rail=PaymentRail.CARD,
        transaction_type=TransactionType.PAYMENT,
    )

    result = await fraud_engine.evaluate(
        merchant_id=current.id,
        payload=transaction_payload,
    )

    return FraudTestResult(
        scenario=payload.scenario,
        fraud_score=result.score,
        flags=result.flags,
        should_block=result.should_block,
        should_review=result.should_review,
        reasons=result.reasons,
    )


@router.post("/webhook", response_model=WebhookTestResult)
async def test_webhook(
    payload: WebhookTestRequest,
    current: CurrentMerchant,
    db: DBSession,
) -> WebhookTestResult:
    """
    Manually fires a webhook event for a transaction.
    Use force_fail to simulate a failed delivery and
    watch the retry system kick in.
    """
    # Verify transaction belongs to merchant
    txn_result = await db.execute(
        select(Transaction).where(
            Transaction.id == payload.transaction_id,
            Transaction.merchant_id == current.id,
        )
    )
    transaction = txn_result.scalar_one_or_none()

    if transaction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found",
        )

    dispatcher = WebhookDispatcher(db)
    deliveries = await dispatcher.dispatch(
        transaction=transaction,
        event_type=payload.event_type,
    )

    if not deliveries:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active webhook endpoints found for this merchant",
        )

    delivery = deliveries[0]

    return WebhookTestResult(
        delivery_id=str(delivery.id),
        event_type=delivery.event_type.value,
        status=delivery.status.value,
        response_status_code=delivery.response_status_code,
        duration_ms=delivery.duration_ms,
        error_message=delivery.error_message,
    )


@router.get("/lock-status/{currency}")
async def get_lock_status(
    currency: str,
    current: CurrentMerchant,
    redis: RedisClient,
) -> dict[str, bool | str]:
    lock_manager = LockManager(redis)
    is_locked = await lock_manager.is_locked(
        f"wallet:{current.id}:{currency}"
    )
    return {"is_locked": is_locked, "resource": f"wallet:{current.id}:{currency}"}
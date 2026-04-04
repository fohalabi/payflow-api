from __future__ import annotations
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from pydantic import BaseModel
from decimal import Decimal
from datetime import datetime

from app.api.router import CurrentMerchant, DBSession, RedisClient
from app.models.account import Account, AccountType, AccountStatus
from app.models.transaction import TransactionType, PaymentRail
from app.schemas.transaction import TransactionCreate, TransactionResponse
from app.services.transaction_engine import TransactionEngine, TransactionError

router = APIRouter(prefix="/wallet", tags=["wallet"])


class AccountResponse(BaseModel):
    id: UUID
    account_type: AccountType
    currency: str
    cached_balance: Decimal
    status: AccountStatus
    created_at: datetime

    model_config = {"from_attributes": True}


class BalanceSummary(BaseModel):
    accounts: list[AccountResponse]
    total_by_currency: dict[str, Decimal]


class TransferRequest(BaseModel):
    to_merchant_id: UUID
    amount: Decimal
    currency: str
    idempotency_key: str | None = None
    description: str | None = None

@router.get("/balance", response_model=BalanceSummary)
async def get_balance(
    current: CurrentMerchant,
    db: DBSession,
) -> BalanceSummary:
    result = await db.execute(
        select(Account).where(
            Account.merchant_id == current.id,
            Account.account_type == AccountType.WALLET,
        )
    )
    accounts = result.scalars().all()

    total_by_currency: dict[str, Decimal] = {}
    for account in accounts:
        total_by_currency[account.currency] = (
            total_by_currency.get(account.currency, Decimal("0"))
            + account.cached_balance
        )

    return BalanceSummary(
        accounts=[AccountResponse.model_validate(a) for a in accounts],
        total_by_currency=total_by_currency,
    )


@router.get("/accounts", response_model=list[AccountResponse])
async def list_accounts(
    current: CurrentMerchant,
    db: DBSession,
) -> list[AccountResponse]:
    result = await db.execute(
        select(Account).where(Account.merchant_id == current.id)
    )
    accounts = result.scalars().all()
    return [AccountResponse.model_validate(a) for a in accounts]


@router.get("/accounts/{account_id}", response_model=AccountResponse)
async def get_account(
    account_id: UUID,
    current: CurrentMerchant,
    db: DBSession,
) -> AccountResponse:
    result = await db.execute(
        select(Account).where(
            Account.id == account_id,
            Account.merchant_id == current.id,
        )
    )
    account = result.scalar_one_or_none()

    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found",
        )

    return AccountResponse.model_validate(account)


@router.post("/transfer", response_model=TransactionResponse)
async def transfer(
    payload: TransferRequest,
    current: CurrentMerchant,
    db: DBSession,
    redis: RedisClient,
) -> TransactionResponse:
    # Verify destination merchant exists
    from app.models.merchant import Merchant
    dest_result = await db.execute(
        select(Merchant).where(Merchant.id == payload.to_merchant_id)
    )
    destination = dest_result.scalar_one_or_none()

    if destination is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Destination merchant not found",
        )

    if destination.id == current.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot transfer to yourself",
        )

    engine = TransactionEngine(db, redis)

    transaction_payload = TransactionCreate(
        amount=payload.amount,
        currency=payload.currency,
        payment_rail=PaymentRail.WALLET,
        transaction_type=TransactionType.TRANSFER,
        idempotency_key=payload.idempotency_key,
        metadata={
            "from_wallet": str(current.id),
            "to_wallet": str(payload.to_merchant_id),
        },
        description=payload.description,
    )

    try:
        result, _ = await engine.process_payment(
            merchant_id=current.id,
            payload=transaction_payload,
        )
    except TransactionError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    from app.models.transaction import Transaction
    from sqlalchemy import select as sa_select
    txn_result = await db.execute(
        sa_select(Transaction).where(
            Transaction.reference == result["reference"]
        )
    )
    transaction = txn_result.scalar_one()

    return TransactionResponse.model_validate(transaction)
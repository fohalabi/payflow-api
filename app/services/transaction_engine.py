from __future__ import annotations
import logging
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis

from app.core.config import settings
from app.models.account import Account, AccountType
from app.models.merchant import Merchant
from app.models.transaction import Transaction, TransactionStatus, TransactionType, PaymentRail
from app.schemas.transaction import TransactionCreate
from app.services.idempotency import IdempotencyService, ConcurrentRequestError
from app.services.ledger import LedgerService, InsufficientFundsError
from app.services.lock_manager import LockManager, LockAcquisitionError
from app.services.fraud_engine import FraudEngine
from app.services.webhook_dispatcher import WebhookDispatcher
from app.models.webhook import WebhookEventType

logger = logging.getLogger(__name__)


# Exceptions

class TransactionError(Exception):
    pass

class FraudBlockedError(TransactionError):
    pass

class MerchantNotFoundError(TransactionError):
    pass

class AccountNotFoundError(TransactionError):
    pass


# Fee calculation 

def calculate_fee(amount: Decimal, rail: PaymentRail) -> Decimal:
    rates = {
        PaymentRail.CARD:          Decimal("0.015"),   # 1.5%
        PaymentRail.BANK_TRANSFER: Decimal("0.01"),    # 1.0%
        PaymentRail.WALLET:        Decimal("0.005"),   # 0.5%
        PaymentRail.CRYPTO:        Decimal("0.02"),    # 2.0%
    }
    rate = rates.get(rail, Decimal("0.015"))
    return (amount * rate).quantize(Decimal("0.01"))


def generate_reference() -> str:
    return f"TXN-{secrets.token_hex(6).upper()}"


# Transaction engine 

class TransactionEngine:

    def __init__(self, db: AsyncSession, redis: Redis) -> None:
        self.db = db
        self.redis = redis
        self.ledger = LedgerService(db)
        self.lock_manager = LockManager(redis)
        self.fraud_engine = FraudEngine(db, redis)
        self.idempotency = IdempotencyService(redis)
        self.webhook = WebhookDispatcher(db)

    async def _get_merchant(self, merchant_id: UUID) -> Merchant:
        result = await self.db.execute(
            select(Merchant).where(Merchant.id == merchant_id)
        )
        merchant = result.scalar_one_or_none()
        if not merchant:
            raise MerchantNotFoundError(f"Merchant {merchant_id} not found")
        return merchant

    async def _get_account(
        self,
        merchant_id: UUID,
        account_type: AccountType,
        currency: str,
    ) -> Account:
        result = await self.db.execute(
            select(Account).where(
                Account.merchant_id == merchant_id,
                Account.account_type == account_type,
                Account.currency == currency,
            )
        )
        account = result.scalar_one_or_none()
        if not account:
            raise AccountNotFoundError(
                f"No {account_type.value} account found for "
                f"merchant {merchant_id} in {currency}"
            )
        return account

    async def _get_platform_escrow(self, currency: str) -> Account:
        result = await self.db.execute(
            select(Account).where(
                Account.account_type == AccountType.ESCROW,
                Account.currency == currency,
                Account.is_system_account.is_(True),     # noqa: E712
            )
        )
        account = result.scalar_one_or_none()
        if not account:
            raise AccountNotFoundError(
                f"No platform escrow account for currency {currency}"
            )
        return account

    async def _do_process(
        self,
        merchant_id: UUID,
        payload: TransactionCreate,
    ) -> dict:
        """
        Core processing logic — called inside the idempotency wrapper.
        Creates the transaction, scores it, locks, posts ledger entries.
        """
        merchant = await self._get_merchant(merchant_id)

        # Step 1 — Fraud check
        fraud_result = await self.fraud_engine.evaluate(merchant_id, payload)

        fee_amount = calculate_fee(payload.amount, payload.payment_rail)
        net_amount = payload.amount - fee_amount

        # Step 2 — Create transaction record
        transaction = Transaction(
            reference=generate_reference(),
            merchant_id=merchant_id,
            amount=payload.amount,
            currency=payload.currency,
            fee_amount=fee_amount,
            net_amount=net_amount,
            transaction_type=payload.transaction_type,
            payment_rail=payload.payment_rail,
            status=TransactionStatus.INITIATED,
            idempotency_key=payload.idempotency_key,
            fraud_score=fraud_result.score,
            fraud_flags=fraud_result.flags if fraud_result.flags else None,
            metadata_=payload.metadata,
        )
        self.db.add(transaction)
        await self.db.flush()

        # Step 3 — Block if fraud score too high
        if fraud_result.should_block:
            transaction.status = TransactionStatus.FLAGGED
            await self.webhook.dispatch(transaction, WebhookEventType.PAYMENT_FLAGGED)
            raise FraudBlockedError(
                f"Transaction blocked by fraud engine. "
                f"Score: {fraud_result.score}. "
                f"Flags: {list(fraud_result.flags.keys())}"
            )

        # Step 4 — Acquire distributed lock on merchant wallet
        async with self.lock_manager.lock(
            f"wallet:{merchant_id}:{payload.currency}"
        ):
            # Step 5 — Move to processing
            transaction.status = TransactionStatus.PROCESSING
            await self.db.flush()
            await self.webhook.dispatch(transaction, WebhookEventType.PAYMENT_PROCESSING)

            try:
                # Step 6 — Post ledger entries
                wallet = await self._get_account(
                    merchant_id, AccountType.WALLET, payload.currency
                )
                escrow = await self._get_platform_escrow(payload.currency)

                await self.ledger.post_double_entry(
                    transaction=transaction,
                    debit_account_id=wallet.id,
                    credit_account_id=escrow.id,
                    amount=payload.amount,
                    description=f"Payment {transaction.reference}",
                )

                # Step 7 — Mark complete
                transaction.status = TransactionStatus.COMPLETED
                transaction.completed_at = datetime.now(timezone.utc)
                await self.db.flush()

                await self.webhook.dispatch(
                    transaction, WebhookEventType.PAYMENT_COMPLETED
                )

            except InsufficientFundsError:
                transaction.status = TransactionStatus.FAILED
                transaction.failure_reason = "Insufficient funds"
                await self.webhook.dispatch(transaction, WebhookEventType.PAYMENT_FAILED)
                raise

            except Exception as e:
                transaction.status = TransactionStatus.FAILED
                transaction.failure_reason = str(e)[:500]
                await self.webhook.dispatch(transaction, WebhookEventType.PAYMENT_FAILED)
                raise

        return {
            "id": str(transaction.id),
            "reference": transaction.reference,
            "status": transaction.status.value,
            "amount": str(transaction.amount),
            "currency": transaction.currency,
            "fraud_score": transaction.fraud_score,
            "fraud_flags": transaction.fraud_flags,
            "fee_amount": str(transaction.fee_amount),
            "net_amount": str(transaction.net_amount),
            "created_at": transaction.created_at.isoformat(),
        }

    async def process_payment(
        self,
        merchant_id: UUID,
        payload: TransactionCreate,
    ) -> tuple[dict, bool]:
        """
        Public entry point. Wraps _do_process with idempotency.
        Returns (result, is_duplicate).
        """
        if not payload.idempotency_key:
            result = await self._do_process(merchant_id, payload)
            return result, False

        key: str = payload.idempotency_key
        return await self.idempotency.process_with_idempotency(
            key,
            str(merchant_id),
            self._do_process,
            merchant_id,
            payload,
        )
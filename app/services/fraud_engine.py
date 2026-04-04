from __future__ import annotations
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.transaction import Transaction, TransactionStatus
from app.schemas.transaction import TransactionCreate

logger = logging.getLogger(__name__)


# Fraud threshold
# Score weights for each rule.
# Total score = sum of all triggered rule weights.
# Scores are additive — multiple small signals
# can combine into a high-risk verdict.

SCORE_WEIGHTS = {
    "velocity_exceeded":        40,   # too many txns in short window
    "high_amount":              20,   # unusually large transaction
    "amount_just_below_limit":  15,   # structuring pattern
    "new_account_high_amount":  20,   # new account, large first txn
    "repeated_failures":        25,   # multiple failed attempts
    "round_amount":             10,   # suspiciously round numbers
    "currency_mismatch":        15,   # currency differs from history
}

# Score thresholds
SCORE_REVIEW   = 40    # flag for manual review
SCORE_BLOCK    = 75    # block automatically


# Fraud result 

@dataclass
class FraudResult:
    """
    The output of running all fraud rules against
    a transaction. Attached to the transaction before
    it is processed.
    """
    score: int = 0
    flags: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    should_block: bool = False
    should_review: bool = False

    def add_flag(self, rule: str, reason: str) -> None:
        weight = SCORE_WEIGHTS.get(rule, 0)
        self.score = min(100, self.score + weight)
        self.flags[rule] = True
        self.reasons.append(reason)
        self.should_block = self.score >= SCORE_BLOCK
        self.should_review = self.score >= SCORE_REVIEW


# Fraud engine 

class FraudEngine:

    def __init__(self, db: AsyncSession, redis: Redis) -> None:
        self.db = db
        self.redis = redis

    def _velocity_key(self, merchant_id: UUID) -> str:
        """
        Redis key for tracking transaction count
        in the current time window.
        """
        return f"fraud:velocity:{merchant_id}"

    async def _check_velocity(
        self,
        merchant_id: UUID,
        result: FraudResult,
    ) -> None:
        """
        Rule 1 — Velocity check.

        Counts how many transactions this merchant has
        initiated in the last N seconds using a Redis
        counter with an expiry window.

        A sudden spike in transaction volume is a strong
        signal of automated fraud or account compromise.
        """
        key = self._velocity_key(merchant_id)
        try:
            # Increment counter and set expiry atomically
            pipe = self.redis.pipeline()
            await pipe.incr(key)
            await pipe.expire(key, settings.FRAUD_VELOCITY_WINDOW)
            results = await pipe.execute()
            count = results[0]

            if count > settings.FRAUD_VELOCITY_LIMIT:
                result.add_flag(
                    "velocity_exceeded",
                    f"Transaction velocity exceeded: {count} transactions "
                    f"in {settings.FRAUD_VELOCITY_WINDOW}s "
                    f"(limit: {settings.FRAUD_VELOCITY_LIMIT})",
                )
                logger.warning(
                    f"Velocity exceeded: merchant={merchant_id} "
                    f"count={count}"
                )
        except Exception as e:
            logger.warning(f"Velocity check failed: {e}")

    async def _check_amount(
        self,
        payload: TransactionCreate,
        result: FraudResult,
    ) -> None:
        """
        Rule 2 — Amount checks.

        Checks for:
        - Unusually large amounts above threshold
        - Structuring: amounts just below round thresholds
          (e.g. $9,999 is suspicious if limit is $10,000)
        - Suspiciously round amounts (often test probes)
        """
        amount = payload.amount
        threshold = Decimal(str(settings.FRAUD_HIGH_AMOUNT_THRESHOLD))

        # High amount check
        if amount > threshold:
            result.add_flag(
                "high_amount",
                f"Transaction amount {amount} exceeds "
                f"high-risk threshold {threshold}",
            )

        # Structuring check — amount just below a round threshold
        # e.g. 9,999 when limit is 10,000
        structuring_thresholds = [
            Decimal("10000"),
            Decimal("5000"),
            Decimal("1000"),
        ]
        for t in structuring_thresholds:
            lower_bound = t * Decimal("0.95")   # within 5% below
            if lower_bound <= amount < t:
                result.add_flag(
                    "amount_just_below_limit",
                    f"Amount {amount} is suspiciously close "
                    f"to threshold {t} — possible structuring",
                )
                break

        # Round amount check
        # Fraudsters often probe systems with round numbers
        if amount % Decimal("1000") == 0 and amount >= Decimal("5000"):
            result.add_flag(
                "round_amount",
                f"Suspiciously round amount: {amount}",
            )

    async def _check_account_history(
        self,
        merchant_id: UUID,
        payload: TransactionCreate,
        result: FraudResult,
    ) -> None:
        """
        Rule 3 — Account history checks.

        Checks for:
        - New accounts attempting high-value transactions
        - Repeated failures suggesting brute force
        - Currency inconsistency vs historical transactions
        """
        # Count total transactions for this merchant
        total_count_result = await self.db.execute(
            select(func.count(Transaction.id))
            .where(Transaction.merchant_id == merchant_id)
        )
        total_count = total_count_result.scalar_one()

        # New account high amount check
        threshold = Decimal(str(settings.FRAUD_HIGH_AMOUNT_THRESHOLD))
        if total_count < 5 and payload.amount > threshold * Decimal("0.5"):
            result.add_flag(
                "new_account_high_amount",
                f"New account ({total_count} prior transactions) "
                f"attempting high-value transaction: {payload.amount}",
            )

        # Repeated failures check
        failure_count_result = await self.db.execute(
            select(func.count(Transaction.id))
            .where(
                Transaction.merchant_id == merchant_id,
                Transaction.status == TransactionStatus.FAILED,
            )
        )
        failure_count = failure_count_result.scalar_one()

        if failure_count >= 3:
            result.add_flag(
                "repeated_failures",
                f"Account has {failure_count} recent failed "
                f"transactions — possible brute force",
            )

        # Currency mismatch check
        if total_count > 0:
            common_currency_result = await self.db.execute(
                select(Transaction.currency)
                .where(Transaction.merchant_id == merchant_id)
                .group_by(Transaction.currency)
                .order_by(func.count(Transaction.currency).desc())
                .limit(1)
            )
            common_currency = common_currency_result.scalar_one_or_none()

            if (
                common_currency
                and common_currency != payload.currency
                and total_count > 10
            ):
                result.add_flag(
                    "currency_mismatch",
                    f"Transaction currency '{payload.currency}' differs "
                    f"from account's typical currency '{common_currency}'",
                )

    async def evaluate(
        self,
        merchant_id: UUID,
        payload: TransactionCreate,
    ) -> FraudResult:
        """
        Run all fraud rules against a transaction.
        Returns a FraudResult with score, flags, and verdict.

        Called before any payment processing begins.
        If result.should_block is True, the transaction
        never reaches the payment rail.
        """
        result = FraudResult()

        # Run all rules — each is independent so a failure
        # in one doesn't stop the others from running.
        await self._check_velocity(merchant_id, result)
        await self._check_amount(payload, result)
        await self._check_account_history(merchant_id, payload, result)

        logger.info(
            f"Fraud evaluation complete: "
            f"merchant={merchant_id} "
            f"score={result.score} "
            f"flags={list(result.flags.keys())} "
            f"block={result.should_block}"
        )

        return result

    async def evaluate_existing(
        self,
        transaction: Transaction,
    ) -> FraudResult:
        """
        Re-evaluate fraud score on an existing transaction.
        Used by the fraud worker for async re-scoring
        and by the simulate panel for demo purposes.
        """
        from app.schemas.transaction import TransactionCreate
        from app.models.transaction import PaymentRail, TransactionType

        payload = TransactionCreate(
            amount=transaction.amount,
            currency=transaction.currency,
            payment_rail=transaction.payment_rail,
            transaction_type=transaction.transaction_type,
        )
        return await self.evaluate(transaction.merchant_id, payload)
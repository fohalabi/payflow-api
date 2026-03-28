from __future__ import annotations
from typing import TYPE_CHECKING
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum as PyEnum

from sqlalchemy import (
    String,
    Numeric,
    DateTime,
    Enum,
    ForeignKey,
    Text,
    Integer,
    text,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.merchant import Merchant
    from app.models.journal_entry import JournalEntry


# ── Payment rail ──────────────────────────────────────────
# Which payment method was used for this transaction.
class PaymentRail(str, PyEnum):
    CARD         = "card"
    BANK_TRANSFER = "bank_transfer"
    WALLET       = "wallet"
    CRYPTO       = "crypto"


# ── Transaction status ────────────────────────────────────
# These are the exact states in the state machine.
# The allowed transitions are enforced in transaction_engine.py
class TransactionStatus(str, PyEnum):
    INITIATED   = "initiated"    # created, not yet processed
    PROCESSING  = "processing"   # sent to payment rail
    PENDING     = "pending"      # awaiting external confirmation
    COMPLETED   = "completed"    # successfully settled
    FAILED      = "failed"       # processing failed
    REVERSED    = "reversed"     # refunded or reversed
    FLAGGED     = "flagged"      # held by fraud engine


# ── Transaction type ──────────────────────────────────────
class TransactionType(str, PyEnum):
    PAYMENT     = "payment"      # customer paying merchant
    TRANSFER    = "transfer"     # wallet to wallet
    WITHDRAWAL  = "withdrawal"   # merchant withdrawing funds
    REFUND      = "refund"       # reversing a payment
    FEE         = "fee"          # platform fee deduction


class Transaction(Base):
    __tablename__ = "transactions"

    # ── Identity ──────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )

    # Human-readable reference e.g. "TXN-2024-XXXXX"
    reference: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        index=True,
    )

    # ── Ownership ─────────────────────────────────────────
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # ── Amount ────────────────────────────────────────────
    amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 8),
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
    )

    # Fee charged by the platform for this transaction
    fee_amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 8),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
    )

    # Amount after fees deducted
    net_amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 8),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
    )

    # ── Classification ────────────────────────────────────
    transaction_type: Mapped[TransactionType] = mapped_column(
        Enum(TransactionType, name="transaction_type_enum"),
        nullable=False,
    )

    payment_rail: Mapped[PaymentRail] = mapped_column(
        Enum(PaymentRail, name="payment_rail_enum"),
        nullable=False,
    )

    # ── State machine ─────────────────────────────────────
    status: Mapped[TransactionStatus] = mapped_column(
        Enum(TransactionStatus, name="transaction_status_enum"),
        nullable=False,
        default=TransactionStatus.INITIATED,
        server_default="initiated",
        index=True,
    )

    # Tracks how many times processing was attempted
    retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    # Human readable failure reason if status is FAILED
    failure_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # ── Idempotency ───────────────────────────────────────
    # Merchant-provided unique key to prevent duplicate charges.
    # Stored here as a last line of defense at the DB level —
    # primary idempotency logic lives in idempotency.py
    idempotency_key: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
        index=True,
    )

    # ── Fraud ─────────────────────────────────────────────
    # Score from 0 (clean) to 100 (high risk)
    fraud_score: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    fraud_flags: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="JSON object of triggered fraud rules e.g. {velocity_exceeded: true}",
    )

    # ── Flexible metadata ─────────────────────────────────
    # Stores rail-specific data — card last four digits,
    # bank account details, crypto tx hash, etc.
    # JSONB is indexed in Postgres so it's queryable.
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata",
        JSONB,
        nullable=True,
    )

    # ── Timestamps ────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("now()"),
        index=True,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("now()"),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # When the transaction reached a terminal state
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # ── Relationships ─────────────────────────────────────
    merchant: Mapped["Merchant"] = relationship(
        "Merchant",
        back_populates="transactions",
        lazy="raise",
    )

    journal_entries: Mapped[list["JournalEntry"]] = relationship(
        "JournalEntry",
        back_populates="transaction",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return (
            f"<Transaction id={self.id} "
            f"ref='{self.reference}' "
            f"amount={self.amount} {self.currency} "
            f"status={self.status}>"
        )
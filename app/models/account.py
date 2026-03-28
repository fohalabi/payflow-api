from __future__ import annotations
from typing import TYPE_CHECKING
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum as PyEnum

from sqlalchemy import (
    String,
    Numeric,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.merchant import Merchant
    from app.models.journal_entry import JournalEntry


# Each type serves a distinct financial purpose.
class AccountType(str, PyEnum):
    WALLET   = "wallet"    # merchant's spendable balance
    ESCROW   = "escrow"    # funds held during processing
    FEES     = "fees"      # platform fee collection
    RESERVE  = "reserve"   # fraud/chargeback reserve


# Account status 
class AccountStatus(str, PyEnum):
    ACTIVE    = "active"
    FROZEN    = "frozen"    # temporarily blocked
    CLOSED    = "closed"    # permanently closed


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )

    # Every account belongs to a merchant.
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Account details 
    account_type: Mapped[AccountType] = mapped_column(
        Enum(AccountType, name="account_type_enum"),
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(3),      # ISO 4217 — e.g. "USD", "NGN", "EUR"
        nullable=False,
    )

    status: Mapped[AccountStatus] = mapped_column(
        Enum(AccountStatus, name="account_status_enum"),
        nullable=False,
        default=AccountStatus.ACTIVE,
        server_default="active",
    )

    # Balance
    # This is a cached value — the source of truth is always
    # the sum of journal entries. We update this atomically
    # alongside every journal entry write so it stays in sync.
    # Using Numeric(20, 8) supports crypto amounts with
    # up to 8 decimal places (e.g. 0.00000001 BTC).
    cached_balance: Mapped[Decimal] = mapped_column(
        Numeric(20, 8),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
    )

    # Safety flags 
    is_system_account: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="System accounts are used internally and cannot be deleted",
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("now()"),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("now()"),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # lazy="raise" forces explicit eager loading — prevents
    # accidental N+1 queries in async code.
    merchant: Mapped["Merchant"] = relationship(
        "Merchant",
        back_populates="accounts",
        lazy="raise",
    )

    journal_entries: Mapped[list["JournalEntry"]] = relationship(
        "JournalEntry",
        back_populates="account",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return (
            f"<Account id={self.id} "
            f"type={self.account_type} "
            f"currency={self.currency} "
            f"balance={self.cached_balance}>"
        )
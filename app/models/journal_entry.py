from __future__ import annotations
from typing import TYPE_CHECKING
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum as PyEnum

from sqlalchemy import (
    Numeric,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    text,
    CheckConstraint,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.account import Account
    from app.models.transaction import Transaction


# The only two possible directions money can move.
class EntryType(str, PyEnum):
    DEBIT  = "debit"   # money leaving an account
    CREDIT = "credit"  # money entering an account


class JournalEntry(Base):
    __tablename__ = "journal_entries"

    __table_args__ = (
        # Amount must always be positive — direction is
        # conveyed by entry_type, not by sign.
        CheckConstraint(
            "amount > 0",
            name="ck_journal_entries_positive_amount",
        ),
        # Composite index for fast balance calculation —
        # "give me all entries for this account" is the
        # most common query against this table.
        Index(
            "ix_journal_entries_account_created",
            "account_id",
            "created_at",
        ),
        # Composite index for fetching all entries
        # belonging to a specific transaction.
        Index(
            "ix_journal_entries_transaction",
            "transaction_id",
            "entry_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    entry_type: Mapped[EntryType] = mapped_column(
        Enum(EntryType, name="entry_type_enum"),
        nullable=False,
    )

    # Always positive — direction is conveyed by entry_type
    amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 8),
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
    )

    # Running balance of the account after this entry.
    # Stored for fast balance history lookups — you can
    # reconstruct the account balance at any point in time
    # without summing all prior entries.
    running_balance: Mapped[Decimal] = mapped_column(
        Numeric(20, 8),
        nullable=False,
    )

    # Audit trail 
    # Human readable description of why this entry exists.
    # e.g. "Payment from customer for order #1234"
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # This is always True — it exists purely as a visual
    # signal in the codebase and DB that these rows are
    # sacred. The application layer enforces no updates/deletes.
    is_immutable: Mapped[bool] = mapped_column(
        nullable=False,
        default=True,
        server_default="true",
    )

    # Timestamp 
    # Only created_at — no updated_at because this row
    # never changes after insert.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("now()"),
    )

    # Relationships 
    account: Mapped["Account"] = relationship(
        "Account",
        back_populates="journal_entries",
        lazy="raise",
    )

    transaction: Mapped["Transaction"] = relationship(
        "Transaction",
        back_populates="journal_entries",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return (
            f"<JournalEntry id={self.id} "
            f"type={self.entry_type} "
            f"amount={self.amount} {self.currency} "
            f"account={self.account_id}>"
        )
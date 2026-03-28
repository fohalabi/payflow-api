from __future__ import annotations
from typing import TYPE_CHECKING
import uuid
from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import (
    String,
    Boolean,
    DateTime,
    Enum,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.account import Account
    from app.models.transaction import Transaction
    from app.models.webhook import WebhookEndpoint




# Merchant status 
class MerchantStatus(str, PyEnum):
    PENDING  = "pending"    # registered but not verified
    ACTIVE   = "active"     # fully operational
    SUSPENDED = "suspended" # temporarily disabled
    CLOSED   = "closed"     # permanently closed


# Merchant tier
# Controls transaction limits and fee structures.
class MerchantTier(str, PyEnum):
    STARTER      = "starter"       # low limits, higher fees
    GROWTH       = "growth"        # mid limits, standard fees
    ENTERPRISE   = "enterprise"    # custom limits and fees


class Merchant(Base):
    __tablename__ = "merchants"

    # Identity 
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )

    # Profile 
    business_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
    )

    # Hashed with bcrypt — never store raw passwords
    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # API Keys
    # Only the hash is stored — the raw key is shown once
    # at generation time and never saved. See security.py.
    api_key_hash: Mapped[str | None] = mapped_column(
        String(64),     # SHA-256 hex digest is always 64 chars
        nullable=True,
        unique=True,
        index=True,
    )

    # Tracks when the key was last rotated
    api_key_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Status & tier 
    status: Mapped[MerchantStatus] = mapped_column(
        Enum(MerchantStatus, name="merchant_status_enum"),
        nullable=False,
        default=MerchantStatus.PENDING,
        server_default="pending",
    )

    tier: Mapped[MerchantTier] = mapped_column(
        Enum(MerchantTier, name="merchant_tier_enum"),
        nullable=False,
        default=MerchantTier.STARTER,
        server_default="starter",
    )

    # Whether this merchant is in test mode.
    # Test mode transactions are processed but never settle.
    is_test_mode: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )

    # Webhook URL where we deliver payment events
    webhook_url: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )

    # Optional description or notes
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
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

    # Relationships 
    accounts: Mapped[list["Account"]] = relationship(
        "Account",
        back_populates="merchant",
        lazy="raise",
    )

    transactions: Mapped[list["Transaction"]] = relationship(
        "Transaction",
        back_populates="merchant",
        lazy="raise",
    )

    webhook_endpoints: Mapped[list["WebhookEndpoint"]] = relationship(
        "WebhookEndpoint",
        back_populates="merchant",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return (
            f"<Merchant id={self.id} "
            f"business='{self.business_name}' "
            f"status={self.status}>"
        )
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
    ForeignKey,
    Integer,
    Text,
    text,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.merchant import Merchant


# ── Webhook event types ───────────────────────────────────
# Every significant state change in the system emits one
# of these events to the merchant's registered endpoint.
class WebhookEventType(str, PyEnum):
    PAYMENT_INITIATED   = "payment.initiated"
    PAYMENT_PROCESSING  = "payment.processing"
    PAYMENT_COMPLETED   = "payment.completed"
    PAYMENT_FAILED      = "payment.failed"
    PAYMENT_REVERSED    = "payment.reversed"
    PAYMENT_FLAGGED     = "payment.flagged"
    TRANSFER_COMPLETED  = "transfer.completed"
    TRANSFER_FAILED     = "transfer.failed"
    FRAUD_DETECTED      = "fraud.detected"


# Delivery status 
class DeliveryStatus(str, PyEnum):
    PENDING     = "pending"     # queued, not yet attempted
    ATTEMPTING  = "attempting"  # currently in flight
    DELIVERED   = "delivered"   # 2xx response received
    FAILED      = "failed"      # non-2xx or timeout
    EXHAUSTED   = "exhausted"   # all retries used up


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"

    # Identity 
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )

    # Ownership 
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Configuration 
    url: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )

    # Shared secret used to sign webhook payloads.
    # Merchant uses this to verify the payload came from us.
    # Stored hashed — same principle as API keys.
    secret_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    # Which events this endpoint is subscribed to.
    # Stored as JSONB array e.g. ["payment.completed", "fraud.detected"]
    # Empty array means subscribed to all events.
    subscribed_events: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
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
    merchant: Mapped["Merchant"] = relationship(
        "Merchant",
        back_populates="webhook_endpoints",
        lazy="raise",
    )

    deliveries: Mapped[list["WebhookDelivery"]] = relationship(
        "WebhookDelivery",
        back_populates="endpoint",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return (
            f"<WebhookEndpoint id={self.id} "
            f"url='{self.url}' "
            f"active={self.is_active}>"
        )


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    __table_args__ = (
        # Fast lookup of all deliveries for a given endpoint
        # ordered by time — powers the webhook log panel.
        Index(
            "ix_webhook_deliveries_endpoint_created",
            "endpoint_id",
            "created_at",
        ),
        # Fast lookup of pending/failed deliveries for
        # the retry worker to pick up.
        Index(
            "ix_webhook_deliveries_status",
            "status",
            "next_retry_at",
        ),
    )

    # Identity 
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )

    # Foreign keys 
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The transaction that triggered this delivery
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Event 
    event_type: Mapped[WebhookEventType] = mapped_column(
        Enum(WebhookEventType, name="webhook_event_type_enum"),
        nullable=False,
    )

    # The exact JSON payload we sent (or will send)
    payload: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
    )

    # Delivery tracking 
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, name="delivery_status_enum"),
        nullable=False,
        default=DeliveryStatus.PENDING,
        server_default="pending",
    )

    # HTTP response code we got back e.g. 200, 404, 500
    response_status_code: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    # First 1000 chars of the response body for debugging
    response_body: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # How long the request took in milliseconds
    duration_ms: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    # Retry tracking
    attempt_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )

    max_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=5,
        server_default="5",
    )

    # When the next retry should be attempted.
    # Null means no retry scheduled (delivered or exhausted).
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    # Why the delivery failed if it did
    error_message: Mapped[str | None] = mapped_column(
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

    attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relationships 
    endpoint: Mapped["WebhookEndpoint"] = relationship(
        "WebhookEndpoint",
        back_populates="deliveries",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return (
            f"<WebhookDelivery id={self.id} "
            f"event={self.event_type} "
            f"status={self.status} "
            f"attempt={self.attempt_number}/{self.max_attempts}>"
        )
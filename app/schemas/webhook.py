from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.models.webhook import WebhookEventType, DeliveryStatus
from app.models.transaction import TransactionStatus, PaymentRail


# Endpoint management

class WebhookEndpointCreate(BaseModel):
    """
    Payload for POST /api/v1/webhooks/endpoints.
    Registers a new URL to receive payment events.
    """
    url: str = Field(
        ...,
        max_length=500,
        description="HTTPS URL to receive webhook events",
        examples=["https://acme.com/webhooks/payflow"],
    )

    subscribed_events: list[WebhookEventType] = Field(
        default_factory=list,
        description=(
            "Events to subscribe to. "
            "Empty list means all events."
        ),
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError(
                "Webhook URL must use HTTPS for security. "
                "HTTP endpoints are not supported."
            )
        return v

    @field_validator("subscribed_events")
    @classmethod
    def validate_no_duplicates(
        cls, v: list[WebhookEventType]
    ) -> list[WebhookEventType]:
        if len(v) != len(set(v)):
            raise ValueError("Duplicate event types are not allowed")
        return v


class WebhookEndpointUpdate(BaseModel):
    """
    Payload for PATCH /api/v1/webhooks/endpoints/{id}.
    All fields optional.
    """
    url: str | None = Field(default=None, max_length=500)
    subscribed_events: list[WebhookEventType] | None = None
    is_active: bool | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.startswith("https://"):
            raise ValueError("Webhook URL must use HTTPS")
        return v


class WebhookEndpointResponse(BaseModel):
    """
    Safe webhook endpoint representation.
    Never includes secret_hash.
    """
    id: UUID
    url: str
    subscribed_events: list[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class WebhookSecretResponse(BaseModel):
    """
    Returned once when an endpoint is first created.
    The raw secret is used by merchants to verify
    webhook signatures — shown once, never stored.
    """
    raw_secret: str = Field(
        description="Use this to verify webhook signatures"
    )
    message: str = (
        "Store this secret securely. "
        "It will not be shown again. "
        "Use it to verify the X-Payflow-Signature header "
        "on incoming webhook requests."
    )


# Event payload 
# This is the exact JSON shape POSTed to merchant servers.

class TransactionEventData(BaseModel):
    """
    Transaction data embedded in a webhook event payload.
    Self-contained — merchant doesn't need to call back
    to get more info about this transaction.
    """
    id: UUID
    reference: str
    amount: Decimal
    currency: str
    fee_amount: Decimal
    net_amount: Decimal
    payment_rail: PaymentRail
    status: TransactionStatus
    fraud_score: int
    metadata: dict | None = None
    created_at: datetime
    completed_at: datetime | None = None


class WebhookEventPayload(BaseModel):
    """
    The full payload POSTed to a merchant's webhook URL.

    Structure mirrors Stripe's event format — familiar
    to any developer who has worked with payment APIs.

    Example:
    {
        "id": "evt_01HX...",
        "event": "payment.completed",
        "api_version": "2024-01-01",
        "created_at": "2024-01-01T12:00:00Z",
        "data": {
            "id": "txn_01HX...",
            "reference": "TXN-2024-XXXXX",
            "amount": "5000.00",
            ...
        }
    }
    """
    id: str = Field(description="Unique event ID — use for deduplication")
    event: WebhookEventType
    api_version: str = "2024-01-01"
    created_at: datetime
    data: TransactionEventData
    merchant_id: UUID


# Delivery tracking 

class WebhookDeliveryResponse(BaseModel):
    """
    Single delivery attempt — shown in the webhook log panel.
    """
    id: UUID
    event_type: WebhookEventType
    status: DeliveryStatus
    response_status_code: int | None
    response_body: str | None
    duration_ms: int | None
    attempt_number: int
    max_attempts: int
    next_retry_at: datetime | None
    error_message: str | None
    created_at: datetime
    attempted_at: datetime | None

    model_config = {"from_attributes": True}


class WebhookDeliveryListResponse(BaseModel):
    """
    Paginated delivery history for a webhook endpoint.
    Powers the webhook log panel in the dashboard.
    """
    items: list[WebhookDeliveryResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class WebhookRetryRequest(BaseModel):
    """
    Payload for POST /api/v1/webhooks/deliveries/{id}/retry.
    Manually triggers a retry of a failed delivery.
    """
    reason: str | None = Field(
        default=None,
        max_length=255,
        description="Optional reason for manual retry",
        examples=["Merchant server was down for maintenance"],
    )
from __future__ import annotations
from decimal import Decimal
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator
import re

from app.models.transaction import (
    TransactionStatus,
    TransactionType,
    PaymentRail,
)


# Supported currencies
SUPPORTED_CURRENCIES = {"USD", "NGN", "EUR", "GBP", "BTC", "ETH", "USDT"}


# Request schemas 

class TransactionCreate(BaseModel):
    """
    Payload a merchant sends to initiate a payment.
    This is what hits POST /api/v1/transactions.
    """
    amount: Decimal = Field(
        ...,
        gt=0,
        decimal_places=8,
        description="Amount to charge. Must be positive.",
        examples=[Decimal("5000.00")],
    )

    currency: str = Field(
        ...,
        min_length=3,
        max_length=3,
        description="ISO 4217 currency code e.g. NGN, USD",
        examples=["NGN"],
    )

    payment_rail: PaymentRail = Field(
        ...,
        description="Payment method to use",
        examples=[PaymentRail.CARD],
    )

    transaction_type: TransactionType = Field(
        default=TransactionType.PAYMENT,
        description="Type of transaction",
    )

    # Merchant-provided idempotency key.
    # If provided, duplicate requests with the same key
    # return the original response without re-processing.
    idempotency_key: str | None = Field(
        default=None,
        max_length=255,
        description="Unique key to prevent duplicate charges",
        examples=["order_12345_attempt_1"],
    )

    # Flexible metadata — card details, bank info, etc.
    metadata: dict | None = Field(
        default=None,
        description="Rail-specific data e.g. card token, bank account",
    )

    description: str | None = Field(
        default=None,
        max_length=500,
        description="Human readable description of the transaction",
    )

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        v = v.upper()
        if v not in SUPPORTED_CURRENCIES:
            raise ValueError(
                f"Unsupported currency '{v}'. "
                f"Supported: {', '.join(sorted(SUPPORTED_CURRENCIES))}"
            )
        return v

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: Decimal) -> Decimal:
        # Minimum transaction amount — 1 unit of any currency
        if v < Decimal("0.00000001"):
            raise ValueError("Amount too small")
        # Maximum single transaction — prevents accidental large charges
        if v > Decimal("1000000"):
            raise ValueError("Amount exceeds maximum single transaction limit")
        return v

    @model_validator(mode="after")
    def validate_crypto_precision(self) -> TransactionCreate:
        # Fiat currencies only need 2 decimal places
        fiat = {"USD", "NGN", "EUR", "GBP"}
        if self.currency in fiat:
            rounded = self.amount.quantize(Decimal("0.01"))
            if rounded != self.amount:
                raise ValueError(
                    f"{self.currency} amounts must have at most 2 decimal places"
                )
        return self


class TransactionFilter(BaseModel):
    """
    Query parameters for listing transactions.
    Used in GET /api/v1/transactions.
    """
    status: TransactionStatus | None = None
    payment_rail: PaymentRail | None = None
    currency: str | None = None
    min_amount: Decimal | None = Field(default=None, gt=0)
    max_amount: Decimal | None = Field(default=None, gt=0)
    from_date: datetime | None = None
    to_date: datetime | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def validate_date_range(self) -> TransactionFilter:
        if self.from_date and self.to_date:
            if self.from_date > self.to_date:
                raise ValueError("from_date must be before to_date")
        return self

    @model_validator(mode="after")
    def validate_amount_range(self) -> TransactionFilter:
        if self.min_amount and self.max_amount:
            if self.min_amount > self.max_amount:
                raise ValueError("min_amount must be less than max_amount")
        return self


# Response schemas 

class JournalEntryResponse(BaseModel):
    """
    Ledger entry as returned in the transaction detail view.
    Shows the raw debit/credit rows to the dashboard.
    """
    id: UUID
    entry_type: str
    amount: Decimal
    currency: str
    running_balance: Decimal
    description: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TransactionResponse(BaseModel):
    """
    Standard transaction response — used in list views
    and as the immediate response to a create request.
    """
    id: UUID
    reference: str
    amount: Decimal
    currency: str
    fee_amount: Decimal
    net_amount: Decimal
    transaction_type: TransactionType
    payment_rail: PaymentRail
    status: TransactionStatus
    fraud_score: int
    fraud_flags: dict | None
    idempotency_key: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class TransactionDetail(TransactionResponse):
    """
    Full transaction detail — includes journal entries.
    Used in the drawer view on the dashboard.
    This is where the double-entry ledger becomes visible.
    """
    journal_entries: list[JournalEntryResponse] = []
    metadata_: dict | None = Field(default=None, alias="metadata")

    model_config = {"from_attributes": True, "populate_by_name": True}


class TransactionListResponse(BaseModel):
    """
    Paginated list of transactions.
    """
    items: list[TransactionResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class IdempotencyResponse(BaseModel):
    """
    Returned when a duplicate idempotency key is detected.
    Tells the merchant their request was already processed
    and returns the original result.
    """
    is_duplicate: bool
    original_transaction: TransactionResponse | None = None
    message: str = "Duplicate request detected — original result returned"
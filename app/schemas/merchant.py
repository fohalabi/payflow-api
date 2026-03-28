from __future__ import annotations
from datetime import datetime
from uuid import UUID

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from app.models.merchant import MerchantStatus, MerchantTier


# Registration 

class MerchantRegister(BaseModel):
    """
    Payload for POST /api/v1/merchants/register.
    """
    business_name: str = Field(
        ...,
        min_length=2,
        max_length=255,
        description="Legal business name",
        examples=["Acme Payments Ltd"],
    )

    email: EmailStr = Field(
        ...,
        description="Business email address",
        examples=["payments@acme.com"],
    )

    password: str = Field(
        ...,
        min_length=8,
        max_length=100,
        description="Must be at least 8 characters",
    )

    confirm_password: str = Field(
        ...,
        description="Must match password",
    )

    webhook_url: str | None = Field(
        default=None,
        max_length=500,
        description="URL to receive payment event notifications",
        examples=["https://acme.com/webhooks/payflow"],
    )

    @field_validator("business_name")
    @classmethod
    def validate_business_name(cls, v: str) -> str:
        # Strip extra whitespace
        v = " ".join(v.split())
        if not v:
            raise ValueError("Business name cannot be empty")
        return v

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        errors = []
        if not any(c.isupper() for c in v):
            errors.append("at least one uppercase letter")
        if not any(c.islower() for c in v):
            errors.append("at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            errors.append("at least one digit")
        if not any(c in "!@#$%^&*()_+-=[]{}|;:,.<>?" for c in v):
            errors.append("at least one special character")
        if errors:
            raise ValueError(
                f"Password must contain: {', '.join(errors)}"
            )
        return v

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.startswith(("http://", "https://")):
            raise ValueError("Webhook URL must start with http:// or https://")
        return v

    @model_validator(mode="after")
    def passwords_match(self) -> MerchantRegister:
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match")
        return self


# Authentication

class MerchantLogin(BaseModel):
    """
    Payload for POST /api/v1/merchants/login.
    """
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    """
    Returned after successful login.
    access_token is a JWT used to authenticate
    dashboard requests.
    """
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 86400    # seconds — 24 hours
    merchant_id: UUID


# Profile 

class MerchantResponse(BaseModel):
    """
    Public merchant profile — safe to return in API responses.
    Never includes password_hash or api_key_hash.
    """
    id: UUID
    business_name: str
    email: str
    status: MerchantStatus
    tier: MerchantTier
    is_test_mode: bool
    webhook_url: str | None
    description: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MerchantUpdate(BaseModel):
    """
    Payload for PATCH /api/v1/merchants/me.
    All fields optional — only provided fields are updated.
    """
    business_name: str | None = Field(
        default=None,
        min_length=2,
        max_length=255,
    )

    webhook_url: str | None = Field(
        default=None,
        max_length=500,
    )

    description: str | None = Field(
        default=None,
        max_length=1000,
    )

    is_test_mode: bool | None = None

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.startswith(("http://", "https://")):
            raise ValueError("Webhook URL must start with http:// or https://")
        return v


class PasswordChange(BaseModel):
    """
    Payload for POST /api/v1/merchants/me/password.
    """
    current_password: str
    new_password: str = Field(min_length=8, max_length=100)
    confirm_new_password: str

    @model_validator(mode="after")
    def passwords_match(self) -> PasswordChange:
        if self.new_password != self.confirm_new_password:
            raise ValueError("New passwords do not match")
        return self


# API Key management

class ApiKeyResponse(BaseModel):
    """
    Returned immediately after API key generation.
    raw_key is shown ONCE and never stored — merchant
    must copy it now. Subsequent requests only return
    the prefix so the merchant can identify their key.
    """
    raw_key: str = Field(
        description="Full API key — shown once, copy it now"
    )
    key_prefix: str = Field(
        description="First 12 chars — safe to display anytime"
    )
    created_at: datetime
    message: str = (
        "Store this key securely. "
        "It will not be shown again."
    )


class ApiKeyInfo(BaseModel):
    """
    Safe representation of an API key for display
    after initial generation. Never includes the raw key.
    """
    key_prefix: str
    created_at: datetime | None
    is_active: bool
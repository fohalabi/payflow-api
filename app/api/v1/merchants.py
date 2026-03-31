from __future__ import annotations
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from passlib.context import CryptContext
from sqlalchemy import select

from app.api.deps import CurrentMerchant, DBSession
from app.core.security import (
    create_access_token,
    generate_api_key,
    hash_api_key,
)
from app.models.merchant import Merchant, MerchantStatus
from app.schemas.merchant import (
    ApiKeyInfo,
    ApiKeyResponse,
    MerchantLogin,
    MerchantRegister,
    MerchantResponse,
    MerchantUpdate,
    PasswordChange,
    TokenResponse,
)

router = APIRouter(prefix="/merchants", tags=["merchants"])
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


@router.post(
    "/register",
    response_model=MerchantResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    payload: MerchantRegister,
    db: DBSession,
) -> MerchantResponse:
    # Check email uniqueness
    existing = await db.execute(
        select(Merchant).where(Merchant.email == payload.email)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    merchant = Merchant(
        business_name=payload.business_name,
        email=payload.email,
        password_hash=pwd_context.hash(payload.password),
        webhook_url=payload.webhook_url,
        status=MerchantStatus.ACTIVE,
    )
    db.add(merchant)
    await db.flush()
    return MerchantResponse.model_validate(merchant)


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: MerchantLogin,
    db: DBSession,
) -> TokenResponse:
    result = await db.execute(
        select(Merchant).where(Merchant.email == payload.email)
    )
    merchant = result.scalar_one_or_none()

    if not merchant or not pwd_context.verify(
        payload.password, merchant.password_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    if merchant.status != MerchantStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Account is {merchant.status.value}",
        )

    token = create_access_token(subject=str(merchant.id))
    return TokenResponse(
        access_token=token,
        merchant_id=merchant.id,
    )


@router.get("/me", response_model=MerchantResponse)
async def get_profile(current: CurrentMerchant) -> MerchantResponse:
    return MerchantResponse.model_validate(current)


@router.patch("/me", response_model=MerchantResponse)
async def update_profile(
    payload: MerchantUpdate,
    current: CurrentMerchant,
    db: DBSession,
) -> MerchantResponse:
    if payload.business_name is not None:
        current.business_name = payload.business_name
    if payload.webhook_url is not None:
        current.webhook_url = payload.webhook_url
    if payload.description is not None:
        current.description = payload.description
    if payload.is_test_mode is not None:
        current.is_test_mode = payload.is_test_mode

    await db.flush()
    return MerchantResponse.model_validate(current)


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: PasswordChange,
    current: CurrentMerchant,
    db: DBSession,
) -> None:
    if not pwd_context.verify(payload.current_password, current.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )
    current.password_hash = pwd_context.hash(payload.new_password)
    await db.flush()


@router.post("/me/api-keys", response_model=ApiKeyResponse)
async def generate_key(
    current: CurrentMerchant,
    db: DBSession,
) -> ApiKeyResponse:
    raw_key, hashed = generate_api_key(live=not current.is_test_mode)
    current.api_key_hash = hashed
    current.api_key_created_at = datetime.now(timezone.utc)
    await db.flush()

    return ApiKeyResponse(
        raw_key=raw_key,
        key_prefix=raw_key[:12],
        created_at=current.api_key_created_at,
    )


@router.get("/me/api-keys", response_model=ApiKeyInfo)
async def get_key_info(current: CurrentMerchant) -> ApiKeyInfo:
    return ApiKeyInfo(
        key_prefix=current.api_key_hash[:12] if current.api_key_hash else "none",
        created_at=current.api_key_created_at,
        is_active=current.api_key_hash is not None,
    )
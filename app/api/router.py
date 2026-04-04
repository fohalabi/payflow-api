from __future__ import annotations
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis
from app.core.security import decode_access_token
from app.models.merchant import Merchant, MerchantStatus

bearer_scheme = HTTPBearer()


async def get_current_merchant(
    credentials: Annotated[
        HTTPAuthorizationCredentials,
        Security(bearer_scheme),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Merchant:
    token = credentials.credentials
    payload = decode_access_token(token)

    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    merchant_id: str | None = payload.get("sub")
    if merchant_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )

    result = await db.execute(
        select(Merchant).where(Merchant.id == UUID(merchant_id))
    )
    merchant = result.scalar_one_or_none()

    if merchant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Merchant not found",
        )

    if merchant.status == MerchantStatus.SUSPENDED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Merchant account is suspended",
        )

    if merchant.status == MerchantStatus.CLOSED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Merchant account is closed",
        )

    return merchant


# Reusable type aliases 
CurrentMerchant = Annotated[Merchant, Depends(get_current_merchant)]
DBSession = Annotated[AsyncSession, Depends(get_db)]
RedisClient = Annotated[Redis, Depends(get_redis)]

from fastapi import APIRouter
from app.api.v1 import merchants, transaction, webhooks, wallets, simulate

router = APIRouter()
router.include_router(merchants.router)
router.include_router(transaction.router)
router.include_router(webhooks.router)
router.include_router(wallets.router)
router.include_router(simulate.router)
from __future__ import annotations
import secrets
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select, func

from app.api.router import CurrentMerchant, DBSession, RedisClient
from app.core.security import hash_api_key
from app.models.webhook import WebhookDelivery, WebhookEndpoint
from app.schemas.webhook import (
    WebhookDeliveryListResponse,
    WebhookDeliveryResponse,
    WebhookEndpointCreate,
    WebhookEndpointResponse,
    WebhookEndpointUpdate,
    WebhookRetryRequest,
    WebhookSecretResponse,
)
from app.services.webhook_dispatcher import WebhookDispatcher

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post(
    "/endpoints",
    status_code=status.HTTP_201_CREATED,
)
async def create_endpoint(
    payload: WebhookEndpointCreate,
    current: CurrentMerchant,
    db: DBSession,
) -> WebhookSecretResponse:
    raw_secret = secrets.token_hex(32)

    endpoint = WebhookEndpoint(
        merchant_id=current.id,
        url=payload.url,
        secret_hash=hash_api_key(raw_secret),
        subscribed_events=[e.value for e in payload.subscribed_events],
    )
    db.add(endpoint)
    await db.flush()

    return WebhookSecretResponse(raw_secret=raw_secret)


@router.get("/endpoints", response_model=list[WebhookEndpointResponse])
async def list_endpoints(
    current: CurrentMerchant,
    db: DBSession,
) -> list[WebhookEndpointResponse]:
    result = await db.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.merchant_id == current.id
        )
    )
    endpoints = result.scalars().all()
    return [WebhookEndpointResponse.model_validate(e) for e in endpoints]


@router.patch(
    "/endpoints/{endpoint_id}",
    response_model=WebhookEndpointResponse,
)
async def update_endpoint(
    endpoint_id: UUID,
    payload: WebhookEndpointUpdate,
    current: CurrentMerchant,
    db: DBSession,
) -> WebhookEndpointResponse:
    result = await db.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == endpoint_id,
            WebhookEndpoint.merchant_id == current.id,
        )
    )
    endpoint = result.scalar_one_or_none()

    if endpoint is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook endpoint not found",
        )

    if payload.url is not None:
        endpoint.url = payload.url
    if payload.subscribed_events is not None:
        endpoint.subscribed_events = [
            e.value for e in payload.subscribed_events
        ]
    if payload.is_active is not None:
        endpoint.is_active = payload.is_active

    await db.flush()
    return WebhookEndpointResponse.model_validate(endpoint)


@router.delete(
    "/endpoints/{endpoint_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_endpoint(
    endpoint_id: UUID,
    current: CurrentMerchant,
    db: DBSession,
) -> None:
    result = await db.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == endpoint_id,
            WebhookEndpoint.merchant_id == current.id,
        )
    )
    endpoint = result.scalar_one_or_none()

    if endpoint is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook endpoint not found",
        )

    await db.delete(endpoint)


@router.get(
    "/endpoints/{endpoint_id}/deliveries",
    response_model=WebhookDeliveryListResponse,
)
async def list_deliveries(
    endpoint_id: UUID,
    current: CurrentMerchant,
    db: DBSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> WebhookDeliveryListResponse:
    # Verify endpoint belongs to merchant
    ep_result = await db.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == endpoint_id,
            WebhookEndpoint.merchant_id == current.id,
        )
    )
    if ep_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook endpoint not found",
        )

    base_query = select(WebhookDelivery).where(
        WebhookDelivery.endpoint_id == endpoint_id
    )

    count_result = await db.execute(
        select(func.count()).select_from(base_query.subquery())
    )
    total = count_result.scalar_one()

    offset = (page - 1) * page_size
    result = await db.execute(
        base_query
        .order_by(WebhookDelivery.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    deliveries = result.scalars().all()

    return WebhookDeliveryListResponse(
        items=[WebhookDeliveryResponse.model_validate(d) for d in deliveries],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=-(-total // page_size),
    )


@router.post(
    "/deliveries/{delivery_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_delivery(
    delivery_id: UUID,
    payload: WebhookRetryRequest,
    current: CurrentMerchant,
    db: DBSession,
) -> dict[str, str]:
    dispatcher = WebhookDispatcher(db)
    success = await dispatcher.retry_delivery(delivery_id)

    return {
        "status": "retried" if success else "failed",
        "delivery_id": str(delivery_id),
    }
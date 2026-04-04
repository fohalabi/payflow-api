from __future__ import annotations
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.config import settings
from app.models.webhook import (
    WebhookDelivery,
    WebhookEndpoint,
    WebhookEventType,
    DeliveryStatus,
)
from app.models.transaction import Transaction
from app.schemas.webhook import WebhookEventPayload, TransactionEventData

logger = logging.getLogger(__name__)


# Payload signing 

def sign_payload(raw_secret: str, payload_body: str) -> str:
    """
    Sign a webhook payload using HMAC-SHA256.

    The signature is included in the X-Payflow-Signature
    header so merchants can verify the request came from
    us and the payload wasn't tampered with in transit.

    Signature format: "sha256=<hex_digest>"
    This matches Stripe's format — familiar to developers.
    """
    signature = hmac.new(
        raw_secret.encode("utf-8"),
        payload_body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={signature}"


def build_payload(
    event_type: WebhookEventType,
    transaction: Transaction,
) -> WebhookEventPayload:
    """
    Build the standardized event payload from a transaction.
    This is the exact JSON structure merchants receive.
    """
    return WebhookEventPayload(
        id=f"evt_{uuid4().hex}",
        event=event_type,
        api_version="2024-01-01",
        created_at=datetime.now(timezone.utc),
        merchant_id=transaction.merchant_id,
        data=TransactionEventData(
            id=transaction.id,
            reference=transaction.reference,
            amount=transaction.amount,
            currency=transaction.currency,
            fee_amount=transaction.fee_amount,
            net_amount=transaction.net_amount,
            payment_rail=transaction.payment_rail,
            status=transaction.status,
            fraud_score=transaction.fraud_score,
            metadata=transaction.metadata_,
            created_at=transaction.created_at,
            completed_at=transaction.completed_at,
        ),
    )


def serialize_payload(payload: WebhookEventPayload) -> str:
    """
    Serialize payload to a stable JSON string.
    Uses a custom encoder for Decimal, UUID, datetime types.
    sort_keys=True ensures the signature is deterministic —
    same payload always produces the same signature.
    """
    def encoder(obj: object) -> str:
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, UUID):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Cannot serialize {type(obj)}")

    return json.dumps(
        payload.model_dump(mode="json"),
        default=encoder,
        sort_keys=True,
    )


# Retry timing 

def calculate_next_retry(attempt_number: int) -> datetime:
    """
    Exponential backoff for retry scheduling.

    Attempt 1 → 10s
    Attempt 2 → 30s
    Attempt 3 → 90s
    Attempt 4 → 270s (~4.5 min)
    Attempt 5 → exhausted

    Capped at 1 hour to prevent very long waits.
    """
    base = settings.WEBHOOK_INITIAL_BACKOFF    # 10 seconds
    delay = min(base * (3 ** (attempt_number - 1)), 3600)
    return datetime.now(timezone.utc) + timedelta(seconds=delay)


# Webhook dispatcher 

class WebhookDispatcher:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.timeout = httpx.Timeout(
            connect=5.0,    # 5s to establish connection
            read=10.0,      # 10s to read response
            write=5.0,
            pool=5.0,
        )

    async def _get_active_endpoints(
        self,
        merchant_id: UUID,
        event_type: WebhookEventType,
    ) -> list[WebhookEndpoint]:
        """
        Get all active endpoints for a merchant that are
        subscribed to this event type.
        An empty subscribed_events list means all events.
        """
        result = await self.db.execute(
            select(WebhookEndpoint)
            .where(
                WebhookEndpoint.merchant_id == merchant_id,
                WebhookEndpoint.is_active == True,        # noqa: E712
            )
        )
        endpoints = result.scalars().all()

        # Filter by subscription
        subscribed = []
        for endpoint in endpoints:
            subs = endpoint.subscribed_events or []
            if not subs or event_type.value in subs:
                subscribed.append(endpoint)

        return subscribed

    async def _create_delivery(
        self,
        endpoint: WebhookEndpoint,
        transaction: Transaction,
        event_type: WebhookEventType,
        payload_json: str,
    ) -> WebhookDelivery:
        """
        Create a pending delivery record before attempting.
        This ensures every delivery is tracked even if
        the actual HTTP request never completes.
        """
        delivery = WebhookDelivery(
            endpoint_id=endpoint.id,
            transaction_id=transaction.id,
            event_type=event_type,
            payload=json.loads(payload_json),
            status=DeliveryStatus.PENDING,
            attempt_number=1,
            max_attempts=settings.WEBHOOK_MAX_RETRIES,
        )
        self.db.add(delivery)
        await self.db.flush()       # get the ID without committing
        return delivery

    async def _attempt_delivery(
        self,
        delivery: WebhookDelivery,
        endpoint: WebhookEndpoint,
        payload_json: str,
        signature: str,
    ) -> bool:
        """
        Make a single HTTP POST attempt to the endpoint URL.
        Updates the delivery record with the outcome.
        Returns True if delivered successfully.
        """
        delivery.status = DeliveryStatus.ATTEMPTING
        delivery.attempted_at = datetime.now(timezone.utc)

        start_time = time.monotonic()
        success = False

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    endpoint.url,
                    content=payload_json,
                    headers={
                        "Content-Type": "application/json",
                        "X-Payflow-Signature": signature,
                        "X-Payflow-Event": delivery.event_type.value,
                        "X-Payflow-Delivery": str(delivery.id),
                        "User-Agent": "Payflow-Webhooks/1.0",
                    },
                )

            duration_ms = int((time.monotonic() - start_time) * 1000)
            delivery.duration_ms = duration_ms
            delivery.response_status_code = response.status_code

            # Store first 1000 chars of response for debugging
            delivery.response_body = response.text[:1000]

            # 2xx = success
            if 200 <= response.status_code < 300:
                delivery.status = DeliveryStatus.DELIVERED
                delivery.next_retry_at = None
                success = True
                logger.info(
                    f"Webhook delivered: "
                    f"delivery={delivery.id} "
                    f"url={endpoint.url} "
                    f"status={response.status_code} "
                    f"duration={duration_ms}ms"
                )
            else:
                delivery.status = DeliveryStatus.FAILED
                delivery.error_message = (
                    f"Non-2xx response: {response.status_code}"
                )
                logger.warning(
                    f"Webhook delivery failed: "
                    f"delivery={delivery.id} "
                    f"status={response.status_code}"
                )

        except httpx.TimeoutException as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            delivery.duration_ms = duration_ms
            delivery.status = DeliveryStatus.FAILED
            delivery.error_message = f"Timeout after {duration_ms}ms"
            logger.warning(
                f"Webhook timeout: delivery={delivery.id} "
                f"url={endpoint.url}"
            )

        except Exception as e:
            delivery.status = DeliveryStatus.FAILED
            delivery.error_message = str(e)[:500]
            logger.error(
                f"Webhook delivery error: "
                f"delivery={delivery.id} error={e}"
            )

        # Schedule retry if failed and attempts remain
        if not success:
            if delivery.attempt_number < delivery.max_attempts:
                delivery.next_retry_at = calculate_next_retry(
                    delivery.attempt_number
                )
            else:
                delivery.status = DeliveryStatus.EXHAUSTED
                logger.error(
                    f"Webhook exhausted all retries: "
                    f"delivery={delivery.id} "
                    f"url={endpoint.url}"
                )

        return success

    async def dispatch(
        self,
        transaction: Transaction,
        event_type: WebhookEventType,
    ) -> list[WebhookDelivery]:
        """
        Main dispatch method — build payload, sign it,
        and attempt delivery to all subscribed endpoints.

        Called by the transaction engine after every
        significant state change.
        """
        endpoints = await self._get_active_endpoints(
            transaction.merchant_id,
            event_type,
        )

        if not endpoints:
            logger.debug(
                f"No active endpoints for merchant "
                f"{transaction.merchant_id} — skipping"
            )
            return []

        # Build and serialize payload once —
        # same payload goes to all endpoints
        payload = build_payload(event_type, transaction)
        payload_json = serialize_payload(payload)

        deliveries = []
        for endpoint in endpoints:
            try:
                # Sign with this endpoint's secret
                signature = sign_payload(
                    endpoint.secret_hash,
                    payload_json,
                )

                delivery = await self._create_delivery(
                    endpoint,
                    transaction,
                    event_type,
                    payload_json,
                )

                await self._attempt_delivery(
                    delivery,
                    endpoint,
                    payload_json,
                    signature,
                )

                deliveries.append(delivery)

            except Exception as e:
                logger.error(
                    f"Failed to process endpoint "
                    f"{endpoint.id}: {e}"
                )
                continue

        return deliveries

    async def retry_delivery(
        self,
        delivery_id: UUID,
    ) -> bool:
        """
        Retry a specific failed delivery.
        Called by the webhook retry worker and the
        manual retry button in the dashboard.
        """
        result = await self.db.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.id == delivery_id)
        )
        delivery = result.scalar_one_or_none()

        if not delivery:
            logger.error(f"Delivery {delivery_id} not found")
            return False

        if delivery.status == DeliveryStatus.DELIVERED:
            logger.info(f"Delivery {delivery_id} already delivered")
            return True

        # Fetch the endpoint
        endpoint_result = await self.db.execute(
            select(WebhookEndpoint)
            .where(WebhookEndpoint.id == delivery.endpoint_id)
        )
        endpoint = endpoint_result.scalar_one_or_none()

        if not endpoint or not endpoint.is_active:
            logger.warning(
                f"Endpoint for delivery {delivery_id} "
                f"not found or inactive"
            )
            return False

        # Increment attempt number
        delivery.attempt_number += 1
        payload_json = json.dumps(
            delivery.payload,
            sort_keys=True,
        )
        signature = sign_payload(endpoint.secret_hash, payload_json)

        return await self._attempt_delivery(
            delivery,
            endpoint,
            payload_json,
            signature,
        )
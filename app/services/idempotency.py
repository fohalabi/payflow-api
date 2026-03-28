from __future__ import annotations
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from app.core.config import settings

logger = logging.getLogger(__name__)

# Handles types that standard json module can't serialize —
# Decimal (money amounts), UUID (IDs), datetime (timestamps).
class PayflowJSONEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, UUID):
            return str(o)
        if isinstance(o, datetime):
            return o.isoformat()
        return super().default(o)


def _serialize(data: dict) -> str:
    return json.dumps(data, cls=PayflowJSONEncoder)


def _deserialize(data: str) -> dict:
    return json.loads(data)


# Redis key helpers 
def _result_key(idempotency_key: str, merchant_id: str) -> str:
    """
    Namespaced Redis key for storing the result.
    Namespacing by merchant_id prevents key collisions
    between different merchants using the same key string.
    e.g. "idempotency:result:merchant_123:order_456"
    """
    return f"idempotency:result:{merchant_id}:{idempotency_key}"


def _lock_key(idempotency_key: str, merchant_id: str) -> str:
    """
    Separate lock key — prevents two concurrent requests
    with the same idempotency key from both thinking
    they are the first and both processing.
    """
    return f"idempotency:lock:{merchant_id}:{idempotency_key}"


# Core idempotency service 

class IdempotencyService:

    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self.ttl = settings.IDEMPOTENCY_TTL      # 24 hours
        self.lock_ttl = settings.LOCK_TTL         # 10 seconds

    async def get_stored_result(
        self,
        idempotency_key: str,
        merchant_id: str,
    ) -> dict | None:
        """
        Check if we have already processed this request.
        Returns the stored result if found, None if not.

        This is called at the very start of request processing —
        before any database writes or payment rail calls.
        """
        key = _result_key(idempotency_key, merchant_id)
        try:
            stored = await self.redis.get(key)
            if stored:
                logger.info(
                    f"Idempotency hit — key='{idempotency_key}' "
                    f"merchant='{merchant_id}'"
                )
                return _deserialize(stored)
            return None
        except Exception as e:
            # If Redis is unavailable, log and continue —
            # the DB unique constraint is the fallback.
            logger.warning(f"Redis idempotency check failed: {e}")
            return None

    async def acquire_lock(
        self,
        idempotency_key: str,
        merchant_id: str,
    ) -> bool:
        """
        Acquire a short-lived lock on this idempotency key.

        Uses Redis SET NX (set if not exists) — atomic operation.
        Returns True if lock acquired, False if another request
        is already processing this key.

        This prevents the race condition where two concurrent
        requests with the same key both pass the initial check
        and both try to process.
        """
        key = _lock_key(idempotency_key, merchant_id)
        try:
            acquired = await self.redis.set(
                key,
                "locked",
                nx=True,        # only set if key doesn't exist
                ex=self.lock_ttl,
            )
            return acquired is not None
        except Exception as e:
            logger.warning(f"Redis lock acquisition failed: {e}")
            # If Redis is down, allow processing to continue —
            # the DB constraint will catch true duplicates.
            return True

    async def release_lock(
        self,
        idempotency_key: str,
        merchant_id: str,
    ) -> None:
        """
        Release the lock after processing completes.
        Called in a finally block so it always runs,
        even if processing fails.
        """
        key = _lock_key(idempotency_key, merchant_id)
        try:
            await self.redis.delete(key)
        except Exception as e:
            logger.warning(f"Redis lock release failed: {e}")
            # Lock will auto-expire after lock_ttl anyway

    async def store_result(
        self,
        idempotency_key: str,
        merchant_id: str,
        result: dict,
        is_error: bool = False,
    ) -> None:
        """
        Store the result of processing for future duplicate requests.

        Both successful results AND errors are stored —
        if a request fails, a retry with the same key should
        get the same error back, not attempt reprocessing.
        This is the correct idempotency behavior.
        """
        key = _result_key(idempotency_key, merchant_id)
        payload = {
            "result": result,
            "is_error": is_error,
            "stored_at": datetime.utcnow().isoformat(),
        }
        try:
            await self.redis.set(
                key,
                _serialize(payload),
                ex=self.ttl,
            )
        except Exception as e:
            logger.warning(f"Redis result storage failed: {e}")

    async def process_with_idempotency(
        self,
        idempotency_key: str,
        merchant_id: str,
        processor: Any,  # async callable
        *args: Any,
        **kwargs: Any,
    ) -> tuple[dict, bool]:
        """
        High-level idempotency wrapper.

        Usage:
            result, is_duplicate = await idempotency.process_with_idempotency(
                key, merchant_id, process_payment, transaction_data
            )

        Returns a tuple of (result, is_duplicate).
        is_duplicate=True means the result came from cache,
        not from fresh processing.
        """
        # Phase 1 — check for existing result
        stored = await self.get_stored_result(idempotency_key, merchant_id)
        if stored:
            return stored["result"], True

        # Phase 2 — acquire lock to prevent concurrent processing
        lock_acquired = await self.acquire_lock(idempotency_key, merchant_id)
        if not lock_acquired:
            # Another request is currently processing this key.
            # Return a 409 Conflict — client should retry shortly.
            raise ConcurrentRequestError(
                f"Request with key '{idempotency_key}' is already being processed"
            )

        try:
            # Phase 3 — check again after acquiring lock
            # (another request may have completed while we waited)
            stored = await self.get_stored_result(idempotency_key, merchant_id)
            if stored:
                return stored["result"], True

            # Phase 4 — process the request
            is_error = False
            result: dict = {}
            try:
                result = await processor(*args, **kwargs)
            except Exception as e:
                is_error = True
                result = {"error": str(e), "type": type(e).__name__}
                raise
            finally:
                # Phase 5 — store result regardless of success/failure
                await self.store_result(
                    idempotency_key,
                    merchant_id,
                    result if not is_error else result,
                    is_error=is_error,
                )

            return result, False

        finally:
            # Phase 6 — always release the lock
            await self.release_lock(idempotency_key, merchant_id)


# Custom exceptions 

class ConcurrentRequestError(Exception):
    """
    Raised when two requests with the same idempotency key
    arrive simultaneously and one is already being processed.
    """
    pass


class IdempotencyKeyMismatchError(Exception):
    """
    Raised when a merchant reuses an idempotency key
    with different request parameters — this is a merchant
    error, not a duplicate request.
    """
    pass
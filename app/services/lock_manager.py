from __future__ import annotations
import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from redis.asyncio import Redis

from app.core.config import settings

logger = logging.getLogger(__name__)


# Custom exceptions

class LockAcquisitionError(Exception):
    """
    Raised when a distributed lock cannot be acquired
    within the allowed number of retries.
    """
    pass


class LockNotOwnedError(Exception):
    """
    Raised when a process tries to release a lock
    it doesn't own — indicates a serious timing bug.
    """
    pass


# This script runs atomically in Redis — no other command
# can run between the GET and DELETE.
# It checks that the lock token matches before deleting,
# ensuring only the lock owner can release it.
RELEASE_LOCK_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


class DistributedLock:
    """
    A single distributed lock instance.
    Not meant to be used directly — use LockManager instead.
    """

    def __init__(
        self,
        redis: Redis,
        key: str,
        token: str,
        ttl: int,
    ) -> None:
        self.redis = redis
        self.key = key
        self.token = token      
        self.ttl = ttl
        self._released = False

    async def release(self) -> None:
        """
        Release the lock atomically using a Lua script.
        Only releases if this process still owns the lock.
        """
        if self._released:
            return

        result = await self.redis.eval(        # type: ignore[attr-defined]
            RELEASE_LOCK_SCRIPT,
            1,              # number of keys
            self.key,       # KEYS[1]
            self.token,     # ARGV[1]
        )

        self._released = True

        if result == 0:
            # Lock was already expired or taken by someone else.
            # Log it — this means our TTL was too short for
            # the operation we were protecting.
            logger.warning(
                f"Lock release failed — lock expired or "
                f"not owned: key='{self.key}'"
            )
            raise LockNotOwnedError(
                f"Lock '{self.key}' is no longer owned by this process. "
                f"The TTL may be too short for this operation."
            )

    async def extend(self, additional_ttl: int) -> bool:
        """
        Extend the lock TTL if the operation is taking longer
        than expected. Only works if we still own the lock.
        """
        current = await self.redis.get(self.key)
        if current != self.token:
            return False

        await self.redis.pexpire(self.key, additional_ttl * 1000)
        return True


class LockManager:
    """
    High-level distributed lock manager.

    Usage with context manager (recommended):

        async with lock_manager.lock("wallet:uuid-123") as lock:
            # only one process runs this block at a time
            await debit_wallet(...)

    Usage with manual acquire/release:

        lock = await lock_manager.acquire("wallet:uuid-123")
        try:
            await debit_wallet(...)
        finally:
            await lock.release()
    """

    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self.ttl = settings.LOCK_TTL            # default 10 seconds
        self.retry_count = 3                     # attempts before giving up
        self.retry_delay = 0.1                   # seconds between retries

    def _make_key(self, resource: str) -> str:
        """
        Namespace all lock keys under 'lock:' prefix.
        Makes it easy to find all locks in Redis.
        e.g. "lock:wallet:550e8400-e29b-41d4-a716"
        """
        return f"lock:{resource}"

    async def acquire(
        self,
        resource: str,
        ttl: int | None = None,
        retry_count: int | None = None,
        retry_delay: float | None = None,
    ) -> DistributedLock:
        """
        Acquire a distributed lock on a resource.

        Retries up to retry_count times with retry_delay
        between attempts before raising LockAcquisitionError.

        resource: identifies what is being locked.
                  Use descriptive strings like:
                  "wallet:{uuid}" or "transaction:{uuid}"
        """
        key = self._make_key(resource)
        token = secrets.token_hex(16)   # unique per acquisition
        _ttl = ttl or self.ttl
        _retries = retry_count or self.retry_count
        _delay = retry_delay or self.retry_delay

        for attempt in range(_retries):
            acquired = await self.redis.set(
                key,
                token,
                nx=True,            # set only if not exists
                ex=_ttl,            # auto-expire after ttl seconds
            )

            if acquired:
                logger.debug(
                    f"Lock acquired: key='{key}' "
                    f"token='{token[:8]}...' "
                    f"ttl={_ttl}s "
                    f"attempt={attempt + 1}"
                )
                return DistributedLock(
                    redis=self.redis,
                    key=key,
                    token=token,
                    ttl=_ttl,
                )

            # Lock is held by someone else — wait and retry
            if attempt < _retries - 1:
                wait = _delay * (2 ** attempt)  # exponential backoff
                logger.debug(
                    f"Lock busy, retrying in {wait:.2f}s: "
                    f"key='{key}' attempt={attempt + 1}/{_retries}"
                )
                await asyncio.sleep(wait)

        raise LockAcquisitionError(
            f"Could not acquire lock on '{resource}' "
            f"after {_retries} attempts. "
            f"Another operation is in progress."
        )

    @asynccontextmanager
    async def lock(
        self,
        resource: str,
        ttl: int | None = None,
    ) -> AsyncGenerator[DistributedLock, None]:
        """
        Context manager for distributed locking.
        Guarantees the lock is always released, even
        if the protected code raises an exception.

        async with lock_manager.lock("wallet:123") as lock:
            # protected code here
        """
        acquired_lock = await self.acquire(resource, ttl=ttl)
        try:
            yield acquired_lock
        finally:
            try:
                await acquired_lock.release()
            except LockNotOwnedError:
                # Already logged in release() — don't crash
                # the calling code over a lock warning.
                pass

    async def is_locked(self, resource: str) -> bool:
        """
        Check if a resource is currently locked.
        Useful for the simulate panel in the dashboard
        to show lock contention in real time.
        """
        key = self._make_key(resource)
        result = await self.redis.exists(key)
        return bool(result)

    async def force_release(self, resource: str) -> bool:
        """
        Force release a lock regardless of ownership.
        Only for admin use and testing — never call this
        in normal payment processing code.
        """
        key = self._make_key(resource)
        result = await self.redis.delete(key)
        logger.warning(f"Lock force-released: key='{key}'")
        return bool(result)
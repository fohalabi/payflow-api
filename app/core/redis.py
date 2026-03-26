import redis.asyncio as aioredis
from redis.asyncio import Redis
from typing import AsyncGenerator

from app.core.config import settings

# decode_responses=True means Redis returns strings instead
# of raw bytes — much easier to work with throughout the app.
def create_redis_client() -> Redis:
    return aioredis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=5,   # fail fast if Redis is unreachable
        socket_timeout=5,
        retry_on_timeout=True,
    )

# One client shared across the entire app.
# redis.asyncio handles connection pooling internally —
# you don't need to create a new client per request.
redis_client: Redis = create_redis_client()

# Inject into any route that needs Redis directly:
#   async def my_route(redis: Redis = Depends(get_redis)):
async def get_redis() -> AsyncGenerator[Redis, None]:
    yield redis_client


# Called on app startup to verify Redis is reachable.
# If this fails, the app won't start — better than silent
# failures mid-request.
async def ping_redis() -> bool:
    try:
        return await redis_client.ping()
    except Exception:
        return False


# Called on app shutdown to cleanly close the connection pool.
async def close_redis() -> None:
    await redis_client.aclose()
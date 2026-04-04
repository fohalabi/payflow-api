from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import engine
from app.core.redis import ping_redis, close_redis
from app.api.router import router as api_router

logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Startup
    logger.info("Starting Payflow API...")

    redis_ok = await ping_redis()
    if not redis_ok:
        raise RuntimeError("Redis is not reachable — cannot start")
    logger.info("Redis connection verified")

    logger.info("Payflow API started successfully")

    yield

    # Shutdown
    logger.info("Shutting down Payflow API...")
    await close_redis()
    await engine.dispose()
    logger.info("Shutdown complete")

app = FastAPI(
    title="Payflow API",
    description=(
        "A payment gateway infrastructure showcasing idempotency, "
        "double-entry ledger, distributed locking, fraud detection, "
        "and webhook delivery."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "1.0.0"}
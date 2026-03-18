from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import computed_field
from typing import Literal


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    APP_NAME: str = "Payflow API"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = True

    API_V1_PREFIX: str = "/api/v1"
    SECRET_KEY: str = "change-me-in-production"

    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "payflow"
    POSTGRES_PASSWORD: str = "payflow"
    POSTGRES_DB: str = "payflow_db"

    @computed_field
    @property
    def DATABASE_URL(self) -> str:
        # asyncpg driver for async SQLAlchemy
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @computed_field
    @property
    def DATABASE_URL_SYNC(self) -> str:
        # psycopg2 driver for Alembic migrations (sync)
        return (
            f"postgresql+psycopg2://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str = ""
    REDIS_DB: int = 0

    @computed_field
    @property
    def REDIS_URL(self) -> str:
        if self.REDIS_PASSWORD:
            return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @computed_field
    @property
    def CELERY_BROKER_URL(self) -> str:
        return self.REDIS_URL

    @computed_field
    @property
    def CELERY_RESULT_BACKEND(self) -> str:
        return self.REDIS_URL

    # Idempotency
    # How long (seconds) to keep idempotency keys in Redis
    IDEMPOTENCY_TTL: int = 86400  # 24 hours

    # Distributed Locking
    # Max time (seconds) a Redis lock is held before auto-release
    LOCK_TTL: int = 10

    # Fraud Engine
    # Max transactions allowed per wallet in a rolling window
    FRAUD_VELOCITY_LIMIT: int = 10
    FRAUD_VELOCITY_WINDOW: int = 60  # seconds
    # Transactions above this amount trigger extra scrutiny
    FRAUD_HIGH_AMOUNT_THRESHOLD: float = 10000.00

    # Webhook
    WEBHOOK_MAX_RETRIES: int = 5
    WEBHOOK_INITIAL_BACKOFF: int = 10  # seconds


# Single shared instance — import this everywhere
settings = Settings()
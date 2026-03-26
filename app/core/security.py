import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings


#  Password / key hashing context
# bcrypt is the industry standard for hashing secrets.
# schemes list allows future algorithm migration.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# API Key format 
# prefix_<32 random bytes as hex>
# e.g. pk_live_a3f8c2e1d4b7...  (public key)
#      sk_live_9b2e4f1a8c3d...  (secret key)
API_KEY_PREFIX_TEST = "pk_test"
API_KEY_PREFIX_LIVE = "pk_live"
SECRET_KEY_PREFIX_TEST = "sk_test"
SECRET_KEY_PREFIX_LIVE = "sk_live"


def generate_api_key(live: bool = False) -> tuple[str, str]:
    """
    Generate a new API key pair.

    Returns a tuple of (raw_key, hashed_key).
    - raw_key is shown to the merchant ONCE and never stored.
    - hashed_key is what gets stored in the database.
    """
    prefix = API_KEY_PREFIX_LIVE if live else API_KEY_PREFIX_TEST
    raw_key = f"{prefix}_{secrets.token_hex(32)}"
    hashed_key = hash_api_key(raw_key)
    return raw_key, hashed_key


def hash_api_key(raw_key: str) -> str:
    """
    Hash an API key for storage.
    Uses SHA-256 (not bcrypt) because API keys are long
    random strings — bcrypt's cost is unnecessary here
    and would slow down every authenticated request.
    """
    return hashlib.sha256(raw_key.encode()).hexdigest()


def verify_api_key(raw_key: str, hashed_key: str) -> bool:
    """
    Timing-safe comparison of a raw key against its hash.
    Using secrets.compare_digest prevents timing attacks —
    an attacker can't deduce the key by measuring response time.
    """
    expected_hash = hash_api_key(raw_key)
    return secrets.compare_digest(expected_hash, hashed_key)


# JWT tokens (for dashboard UI) 
def create_access_token(
    subject: str | int,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """
    Create a signed JWT token for dashboard authentication.
    subject is typically the merchant's ID.
    """
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(hours=24)
    )
    payload: dict[str, Any] = {
        "sub": str(subject),
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(
        payload,
        settings.SECRET_KEY,
        algorithm="HS256",
    )


def decode_access_token(token: str) -> dict[str, Any] | None:
    """
    Decode and verify a JWT token.
    Returns the payload if valid, None if expired or tampered.
    """
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=["HS256"],
        )
        return payload
    except JWTError:
        return None
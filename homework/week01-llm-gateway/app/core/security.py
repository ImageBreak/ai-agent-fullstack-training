import hashlib
import hmac
import re

from app.domain.errors import GatewayError


def authenticate(header: str | None, key: str) -> str:
    scheme, _, token = (header or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), key.encode()):
        raise GatewayError("invalid_api_key", 401, "A valid gateway Bearer key is required.")
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def client_request_id(value: str | None) -> str | None:
    return value if value and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value) else None

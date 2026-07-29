from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import jwt

from tourism_backend.config import Settings

_ALGORITHM = "HS256"


def create_access_token(*, user_id: UUID, settings: Settings) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_access_ttl_minutes),
        "jti": str(uuid4()),
        "typ": "access",
    }
    return jwt.encode(payload, settings.jwt_signing_key, algorithm=_ALGORITHM)


def decode_access_token(token: str, *, settings: Settings) -> UUID:
    payload = jwt.decode(
        token,
        settings.jwt_signing_key,
        algorithms=[_ALGORITHM],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
        options={"require": ["sub", "exp", "iat", "iss", "aud", "typ"]},
    )
    if payload.get("typ") != "access":
        raise jwt.InvalidTokenError("invalid token typ")
    return UUID(str(payload["sub"]))

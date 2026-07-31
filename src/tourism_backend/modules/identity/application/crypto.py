from hashlib import sha256
from hmac import compare_digest
from secrets import token_urlsafe


def digest_token(raw: str) -> str:
    return sha256(raw.encode("utf-8")).hexdigest()


def digest_matches(raw: str, expected_digest: str) -> bool:
    return compare_digest(digest_token(raw), expected_digest)


def new_refresh_token() -> str:
    return token_urlsafe(48)


def new_otp_code() -> str:
    # Cryptographically random 4-digit code for future SMS delivery.
    from secrets import randbelow

    return f"{randbelow(10_000):04d}"

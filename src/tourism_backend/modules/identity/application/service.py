from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.config import Settings
from tourism_backend.modules.identity.application.crypto import (
    digest_token,
    new_otp_code,
    new_refresh_token,
)
from tourism_backend.modules.identity.application.schemas import (
    MeOut,
    MePatchIn,
    OtpRequestIn,
    OtpVerifyIn,
    TokenPairOut,
)
from tourism_backend.modules.identity.application.tokens import create_access_token
from tourism_backend.modules.identity.infrastructure.models import (
    AuthOtpChallenge,
    AuthRefreshSession,
    User,
)

_OTP_TTL = timedelta(minutes=10)
_MAX_OTP_ATTEMPTS = 8
_RATE_WINDOW_SEC = 600
_RATE_REQUEST_LIMIT = 8
_RATE_VERIFY_LIMIT = 20


async def _rate_limit(redis: Redis, *, key: str, limit: int) -> None:
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, _RATE_WINDOW_SEC)
    if count > limit:
        raise AppError(
            code="rate_limited",
            message="Too many attempts. Try again later.",
            status_code=429,
        )


async def request_otp(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    payload: OtpRequestIn,
    *,
    client_ip: str,
) -> None:
    await _rate_limit(
        redis,
        key=f"auth:otp:req:ip:{client_ip}",
        limit=_RATE_REQUEST_LIMIT,
    )
    await _rate_limit(
        redis,
        key=f"auth:otp:req:phone:{payload.phone}",
        limit=_RATE_REQUEST_LIMIT,
    )

    # TODO: SMS provider — generate code, send via SMS gateway, store digest only.
    code = new_otp_code()
    challenge = AuthOtpChallenge(
        id=uuid4(),
        phone_e164=payload.phone,
        display_name=payload.display_name,
        code_digest=digest_token(code),
        expires_at=datetime.now(UTC) + _OTP_TTL,
        attempts=0,
        consumed_at=None,
        created_at=datetime.now(UTC),
    )
    session.add(challenge)
    await session.commit()
    # Never log or return the OTP code.


async def _issue_tokens(
    session: AsyncSession,
    *,
    user: User,
    settings: Settings,
    device_label: str | None,
    family_id: UUID | None = None,
) -> TokenPairOut:
    refresh_raw = new_refresh_token()
    family = family_id or uuid4()
    refresh_row = AuthRefreshSession(
        id=uuid4(),
        user_id=user.id,
        token_digest=digest_token(refresh_raw),
        family_id=family,
        device_label=device_label,
        expires_at=datetime.now(UTC) + timedelta(days=settings.jwt_refresh_ttl_days),
        revoked_at=None,
        replaced_by_id=None,
        created_at=datetime.now(UTC),
    )
    session.add(refresh_row)
    await session.commit()
    access = create_access_token(user_id=user.id, settings=settings)
    return TokenPairOut(
        access_token=access,
        refresh_token=refresh_raw,
        expires_in=settings.jwt_access_ttl_minutes * 60,
    )


async def verify_otp(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    payload: OtpVerifyIn,
    *,
    client_ip: str,
) -> TokenPairOut:
    await _rate_limit(
        redis,
        key=f"auth:otp:verify:ip:{client_ip}",
        limit=_RATE_VERIFY_LIMIT,
    )
    await _rate_limit(
        redis,
        key=f"auth:otp:verify:phone:{payload.phone}",
        limit=_RATE_VERIFY_LIMIT,
    )

    if not payload.privacy_accepted or not payload.personal_data_accepted:
        raise AppError(
            code="consents_required",
            message="Privacy and personal data consents are required",
            status_code=400,
        )

    result = await session.execute(
        select(AuthOtpChallenge)
        .where(
            AuthOtpChallenge.phone_e164 == payload.phone,
            AuthOtpChallenge.consumed_at.is_(None),
            AuthOtpChallenge.expires_at > datetime.now(UTC),
        )
        .order_by(AuthOtpChallenge.created_at.desc())
        .limit(1)
    )
    challenge = result.scalar_one_or_none()
    if challenge is None:
        raise AppError(
            code="otp_invalid",
            message="Invalid or expired code",
            status_code=400,
        )

    if challenge.attempts >= _MAX_OTP_ATTEMPTS:
        raise AppError(
            code="otp_invalid",
            message="Invalid or expired code",
            status_code=400,
        )

    accept_any = settings.otp_accept_any_enabled
    code_ok = accept_any or digest_token(payload.code) == challenge.code_digest
    challenge.attempts += 1
    if not code_ok:
        await session.commit()
        raise AppError(
            code="otp_invalid",
            message="Invalid or expired code",
            status_code=400,
        )

    challenge.consumed_at = datetime.now(UTC)
    now = datetime.now(UTC)

    user_result = await session.execute(
        select(User).where(User.phone_e164 == payload.phone).limit(1)
    )
    user = user_result.scalar_one_or_none()
    if user is None:
        user = User(
            id=uuid4(),
            display_name=challenge.display_name,
            phone_e164=payload.phone,
            privacy_accepted_at=now,
            personal_data_accepted_at=now,
        )
        session.add(user)
    else:
        user.display_name = challenge.display_name
        user.privacy_accepted_at = now
        user.personal_data_accepted_at = now

    await session.flush()
    return await _issue_tokens(
        session,
        user=user,
        settings=settings,
        device_label=payload.device_label,
    )


async def refresh_tokens(
    session: AsyncSession,
    settings: Settings,
    refresh_token: str,
) -> TokenPairOut:
    digest = digest_token(refresh_token)
    result = await session.execute(
        select(AuthRefreshSession).where(AuthRefreshSession.token_digest == digest).limit(1)
    )
    row = result.scalar_one_or_none()
    now = datetime.now(UTC)
    if row is None:
        raise AppError(code="refresh_invalid", message="Invalid refresh token", status_code=401)

    if row.revoked_at is not None:
        # Reuse detection: revoke entire family.
        await session.execute(
            update(AuthRefreshSession)
            .where(
                AuthRefreshSession.family_id == row.family_id,
                AuthRefreshSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await session.commit()
        raise AppError(
            code="refresh_reuse",
            message="Refresh token reuse detected",
            status_code=401,
        )

    if row.expires_at <= now:
        row.revoked_at = now
        await session.commit()
        raise AppError(code="refresh_invalid", message="Invalid refresh token", status_code=401)

    user = await session.get(User, row.user_id)
    if user is None:
        raise AppError(code="refresh_invalid", message="Invalid refresh token", status_code=401)

    new_raw = new_refresh_token()
    new_row = AuthRefreshSession(
        id=uuid4(),
        user_id=user.id,
        token_digest=digest_token(new_raw),
        family_id=row.family_id,
        device_label=row.device_label,
        expires_at=now + timedelta(days=settings.jwt_refresh_ttl_days),
        revoked_at=None,
        replaced_by_id=None,
        created_at=now,
    )
    session.add(new_row)
    await session.flush()
    row.revoked_at = now
    row.replaced_by_id = new_row.id
    await session.commit()

    access = create_access_token(user_id=user.id, settings=settings)
    return TokenPairOut(
        access_token=access,
        refresh_token=new_raw,
        expires_in=settings.jwt_access_ttl_minutes * 60,
    )


async def logout(session: AsyncSession, refresh_token: str) -> None:
    digest = digest_token(refresh_token)
    result = await session.execute(
        select(AuthRefreshSession).where(AuthRefreshSession.token_digest == digest).limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return
    now = datetime.now(UTC)
    await session.execute(
        update(AuthRefreshSession)
        .where(
            AuthRefreshSession.family_id == row.family_id,
            AuthRefreshSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    await session.commit()


async def get_me(session: AsyncSession, user_id: UUID) -> MeOut:
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)
    return MeOut(id=str(user.id), display_name=user.display_name, phone=user.phone_e164)


async def patch_me(session: AsyncSession, user_id: UUID, payload: MePatchIn) -> MeOut:
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)
    user.display_name = payload.display_name
    await session.commit()
    return MeOut(id=str(user.id), display_name=user.display_name, phone=user.phone_e164)

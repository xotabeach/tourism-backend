from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.config import Settings
from tourism_backend.modules.identity.application import (
    achievements as achievements_service,
)
from tourism_backend.modules.identity.application import (
    default_profile_media,
)
from tourism_backend.modules.identity.application.crypto import (
    digest_matches,
    digest_token,
    new_otp_code,
    new_refresh_token,
)
from tourism_backend.modules.identity.application.schemas import (
    MeOut,
    MePatchIn,
    OtpRequestIn,
    OtpStartIn,
    OtpStartOut,
    OtpVerifyIn,
    PhoneChangeRequestIn,
    PhoneChangeVerifyIn,
    TokenPairOut,
)
from tourism_backend.modules.identity.application.tokens import create_access_token
from tourism_backend.modules.identity.infrastructure.models import (
    AuthOtpChallenge,
    AuthPhoneChangeChallenge,
    AuthRefreshSession,
    User,
)
from tourism_backend.modules.media.application import service as media_service
from tourism_backend.modules.notifications.application import service as notifications_service

_OTP_TTL = timedelta(minutes=10)
_MAX_OTP_ATTEMPTS = 8
_RATE_WINDOW_SEC = 600
_RATE_REQUEST_LIMIT = 8
_RATE_VERIFY_LIMIT = 20
_OTP_ISSUE_LOCK_SEC = 15


async def _rate_limit(
    redis: Redis,
    *,
    key: str,
    limit: int,
    bypass: bool = False,
) -> None:
    if bypass:
        return
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, _RATE_WINDOW_SEC)
    if count > limit:
        raise AppError(
            code="rate_limited",
            message="Too many attempts. Try again later.",
            status_code=429,
        )


async def _acquire_otp_issue_lock(redis: Redis, key: str) -> bool:
    acquired = await redis.set(key, "1", nx=True, ex=_OTP_ISSUE_LOCK_SEC)
    return bool(acquired)


async def start_otp(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    payload: OtpStartIn,
    *,
    client_ip: str,
) -> OtpStartOut:
    user = await session.scalar(select(User).where(User.phone_e164 == payload.phone).limit(1))
    if user is None and payload.display_name is None:
        return OtpStartOut(
            registration_required=True,
            consents_required=True,
            otp_sent=False,
        )

    display_name = user.display_name if user is not None else payload.display_name
    if display_name is None:  # Defensive: narrowed by the branch above.
        raise AppError(
            code="display_name_required", message="Display name is required", status_code=400
        )
    await request_otp(
        session,
        redis,
        settings,
        OtpRequestIn(phone=payload.phone, display_name=display_name),
        client_ip=client_ip,
    )
    consents_required = (
        user is None or user.privacy_accepted_at is None or user.personal_data_accepted_at is None
    )
    return OtpStartOut(
        registration_required=False,
        consents_required=consents_required,
        otp_sent=True,
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
        bypass=settings.otp_accept_any_enabled,
    )
    await _rate_limit(
        redis,
        key=f"auth:otp:req:phone:{payload.phone}",
        limit=_RATE_REQUEST_LIMIT,
        bypass=settings.otp_accept_any_enabled,
    )

    lock_key = f"auth:otp:issue:{payload.phone}"
    if not await _acquire_otp_issue_lock(redis, lock_key):
        # A concurrent request is already issuing a code for this phone.
        return
    try:
        await _upsert_auth_otp_challenge(session, settings, payload)
    finally:
        await redis.delete(lock_key)


async def _upsert_auth_otp_challenge(
    session: AsyncSession,
    settings: Settings,
    payload: OtpRequestIn,
) -> None:
    """Keep a single live OTP per phone.

    Double-submit from the identity screen (IME Done + button, or a second
    tap) must not create a second readable debug_code / SMS.
    """
    now = datetime.now(UTC)
    result = await session.execute(
        select(AuthOtpChallenge)
        .where(
            AuthOtpChallenge.phone_e164 == payload.phone,
            AuthOtpChallenge.consumed_at.is_(None),
            AuthOtpChallenge.expires_at > now,
        )
        .order_by(AuthOtpChallenge.created_at.desc())
        .limit(1)
    )
    challenge = result.scalar_one_or_none()
    if challenge is None:
        # TODO: SMS provider — deliver `code` via the SMS gateway, then stop
        # persisting debug_code (see AUTH_OTP_STORE_DEBUG_CODE).
        code = new_otp_code()
        challenge = AuthOtpChallenge(
            id=uuid4(),
            phone_e164=payload.phone,
            display_name=payload.display_name,
            code_digest=digest_token(code),
            debug_code=code if settings.otp_store_debug_code_enabled else None,
            expires_at=now + _OTP_TTL,
            attempts=0,
            consumed_at=None,
            created_at=now,
        )
        session.add(challenge)
    else:
        challenge.display_name = payload.display_name
    await session.execute(
        update(AuthOtpChallenge)
        .where(
            AuthOtpChallenge.phone_e164 == payload.phone,
            AuthOtpChallenge.consumed_at.is_(None),
            AuthOtpChallenge.id != challenge.id,
        )
        .values(consumed_at=now)
    )
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
        bypass=settings.otp_accept_any_enabled,
    )
    await _rate_limit(
        redis,
        key=f"auth:otp:verify:phone:{payload.phone}",
        limit=_RATE_VERIFY_LIMIT,
        bypass=settings.otp_accept_any_enabled,
    )

    existing_user = await session.scalar(
        select(User).where(User.phone_e164 == payload.phone).limit(1)
    )
    consents_missing = (
        existing_user is None
        or existing_user.privacy_accepted_at is None
        or existing_user.personal_data_accepted_at is None
    )
    if consents_missing and (not payload.privacy_accepted or not payload.personal_data_accepted):
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
    code_ok = accept_any or digest_matches(payload.code, challenge.code_digest)
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

    user = existing_user
    is_new = user is None
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
        if payload.privacy_accepted:
            user.privacy_accepted_at = user.privacy_accepted_at or now
        if payload.personal_data_accepted:
            user.personal_data_accepted_at = user.personal_data_accepted_at or now

    await session.flush()
    achievement_notif = None
    if is_new:
        await default_profile_media.ensure_default_user_media(session, user.id)
        achievement_notif = await achievements_service.grant_random_starter_achievements(
            session,
            user_id=user.id,
            notify=True,
        )
    tokens = await _issue_tokens(
        session,
        user=user,
        settings=settings,
        device_label=payload.device_label,
    )
    if achievement_notif is not None:
        await notifications_service.maybe_push_notification(
            session,
            settings,
            user_id=user.id,
            kind=achievement_notif.kind,
            title=achievement_notif.title,
            body=achievement_notif.body,
            target_type="achievement",
            target_id=achievement_notif.target_id or user.id,
        )
    return tokens


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
    from tourism_backend.modules.subscriptions.application import service as travel_plus

    user = await travel_plus.refresh_user_travel_plus(session, user)
    return await _me_out(session, user)


async def patch_me(session: AsyncSession, user_id: UUID, payload: MePatchIn) -> MeOut:
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)
    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.notify_push_enabled is not None:
        user.notify_push_enabled = payload.notify_push_enabled
    if payload.notify_sms_enabled is not None:
        user.notify_sms_enabled = payload.notify_sms_enabled
    if payload.notify_haptics_enabled is not None:
        user.notify_haptics_enabled = payload.notify_haptics_enabled
    await session.commit()
    return await _me_out(session, user)


async def _me_out(session: AsyncSession, user: User) -> MeOut:
    from tourism_backend.modules.subscriptions.application.entitlements import (
        policy_for_user,
    )

    media = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=[user.id],
        role="avatar",
    )
    covers = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=[user.id],
        role="cover",
    )
    expires = user.travel_plus_expires_at
    policy = policy_for_user(user)
    return MeOut(
        id=str(user.id),
        display_name=user.display_name,
        phone=user.phone_e164,
        avatar_url=media.get(user.id),
        cover_url=covers.get(user.id),
        notify_push_enabled=user.notify_push_enabled,
        notify_sms_enabled=user.notify_sms_enabled,
        notify_haptics_enabled=user.notify_haptics_enabled,
        travel_plus_active=user.travel_plus_active,
        travel_plus_plan=user.travel_plus_plan,
        travel_plus_expires_at=expires.isoformat() if expires is not None else None,
        ai_chat_enabled=policy.ai_chat_enabled,
        max_route_points=policy.max_route_points,
        alternatives_count=policy.alternatives_count,
        advanced_filters_enabled=policy.advanced_filters_enabled,
    )


async def request_phone_change(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    user_id: UUID,
    payload: PhoneChangeRequestIn,
    *,
    client_ip: str,
) -> None:
    await _rate_limit(
        redis,
        key=f"auth:phone_change:req:ip:{client_ip}",
        limit=_RATE_REQUEST_LIMIT,
        bypass=settings.otp_accept_any_enabled,
    )
    await _rate_limit(
        redis,
        key=f"auth:phone_change:req:user:{user_id}",
        limit=_RATE_REQUEST_LIMIT,
        bypass=settings.otp_accept_any_enabled,
    )

    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)

    if payload.phone == user.phone_e164:
        raise AppError(
            code="phone_unchanged",
            message="New phone must differ from the current number",
            status_code=400,
        )

    existing = await session.execute(select(User).where(User.phone_e164 == payload.phone).limit(1))
    if existing.scalar_one_or_none() is not None:
        raise AppError(
            code="phone_taken",
            message="Phone number is already in use",
            status_code=409,
        )

    lock_key = f"auth:phone_change:issue:{user_id}:{payload.phone}"
    if not await _acquire_otp_issue_lock(redis, lock_key):
        return
    try:
        await _upsert_phone_change_challenge(session, settings, user_id, payload)
    finally:
        await redis.delete(lock_key)


async def _upsert_phone_change_challenge(
    session: AsyncSession,
    settings: Settings,
    user_id: UUID,
    payload: PhoneChangeRequestIn,
) -> None:
    now = datetime.now(UTC)
    result = await session.execute(
        select(AuthPhoneChangeChallenge)
        .where(
            AuthPhoneChangeChallenge.user_id == user_id,
            AuthPhoneChangeChallenge.phone_e164 == payload.phone,
            AuthPhoneChangeChallenge.consumed_at.is_(None),
            AuthPhoneChangeChallenge.expires_at > now,
        )
        .order_by(AuthPhoneChangeChallenge.created_at.desc())
        .limit(1)
    )
    challenge = result.scalar_one_or_none()
    if challenge is None:
        # TODO: SMS provider — deliver `code` via the SMS gateway, then stop
        # persisting debug_code (see AUTH_OTP_STORE_DEBUG_CODE).
        code = new_otp_code()
        challenge = AuthPhoneChangeChallenge(
            id=uuid4(),
            user_id=user_id,
            phone_e164=payload.phone,
            code_digest=digest_token(code),
            debug_code=code if settings.otp_store_debug_code_enabled else None,
            expires_at=now + _OTP_TTL,
            attempts=0,
            consumed_at=None,
            created_at=now,
        )
        session.add(challenge)
    await session.execute(
        update(AuthPhoneChangeChallenge)
        .where(
            AuthPhoneChangeChallenge.user_id == user_id,
            AuthPhoneChangeChallenge.phone_e164 == payload.phone,
            AuthPhoneChangeChallenge.consumed_at.is_(None),
            AuthPhoneChangeChallenge.id != challenge.id,
        )
        .values(consumed_at=now)
    )
    await session.commit()


async def verify_phone_change(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    user_id: UUID,
    payload: PhoneChangeVerifyIn,
    *,
    client_ip: str,
) -> MeOut:
    await _rate_limit(
        redis,
        key=f"auth:phone_change:verify:ip:{client_ip}",
        limit=_RATE_VERIFY_LIMIT,
        bypass=settings.otp_accept_any_enabled,
    )
    await _rate_limit(
        redis,
        key=f"auth:phone_change:verify:user:{user_id}",
        limit=_RATE_VERIFY_LIMIT,
        bypass=settings.otp_accept_any_enabled,
    )

    if not payload.privacy_accepted or not payload.personal_data_accepted:
        raise AppError(
            code="consents_required",
            message="Privacy and personal data consents are required",
            status_code=400,
        )

    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)

    result = await session.execute(
        select(AuthPhoneChangeChallenge)
        .where(
            AuthPhoneChangeChallenge.user_id == user_id,
            AuthPhoneChangeChallenge.phone_e164 == payload.phone,
            AuthPhoneChangeChallenge.consumed_at.is_(None),
            AuthPhoneChangeChallenge.expires_at > datetime.now(UTC),
        )
        .order_by(AuthPhoneChangeChallenge.created_at.desc())
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
    code_ok = accept_any or digest_matches(payload.code, challenge.code_digest)
    challenge.attempts += 1
    if not code_ok:
        await session.commit()
        raise AppError(
            code="otp_invalid",
            message="Invalid or expired code",
            status_code=400,
        )

    taken = await session.execute(
        select(User).where(User.phone_e164 == payload.phone, User.id != user_id).limit(1)
    )
    if taken.scalar_one_or_none() is not None:
        raise AppError(
            code="phone_taken",
            message="Phone number is already in use",
            status_code=409,
        )

    challenge.consumed_at = datetime.now(UTC)
    now = datetime.now(UTC)
    user.phone_e164 = payload.phone
    user.privacy_accepted_at = now
    user.personal_data_accepted_at = now
    await session.commit()
    return await _me_out(session, user)


async def set_user_media_attachment(
    session: AsyncSession,
    user_id: UUID,
    *,
    kind: str,
    storage_key: str,
    content_type: str | None = None,
    byte_size: int | None = None,
    width: int | None = None,
    height: int | None = None,
) -> MeOut:
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)
    if kind not in {"avatar", "cover"}:
        raise AppError(code="validation_error", message="Unknown media kind", status_code=400)
    await media_service.replace_attachment(
        session,
        entity_type="user",
        entity_id=user_id,
        role=kind,
        storage_key=storage_key,
        content_type=content_type,
        byte_size=byte_size,
        width=width,
        height=height,
        uploaded_by_user_id=user_id,
    )
    await session.commit()
    return await _me_out(session, user)

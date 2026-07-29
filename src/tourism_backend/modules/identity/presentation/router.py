from typing import Annotated

from fastapi import APIRouter, File, Request, Response, UploadFile, status

from tourism_backend.api.deps import CurrentUserId, DbSession, RedisClient, SettingsDep
from tourism_backend.modules.identity.application import media as identity_media
from tourism_backend.modules.identity.application import service as identity_service
from tourism_backend.modules.identity.application.schemas import (
    LogoutIn,
    MeOut,
    MePatchIn,
    OtpRequestIn,
    OtpVerifyIn,
    PhoneChangeRequestIn,
    PhoneChangeVerifyIn,
    RefreshIn,
    TokenPairOut,
)

router = APIRouter(tags=["auth"])


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:64] or "unknown"
    if request.client and request.client.host:
        return request.client.host[:64]
    return "unknown"


@router.post("/auth/otp/request", status_code=status.HTTP_204_NO_CONTENT)
async def otp_request(
    payload: OtpRequestIn,
    request: Request,
    session: DbSession,
    redis: RedisClient,
    settings: SettingsDep,
) -> Response:
    await identity_service.request_otp(
        session,
        redis,
        settings,
        payload,
        client_ip=_client_ip(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/auth/otp/verify", response_model=TokenPairOut)
async def otp_verify(
    payload: OtpVerifyIn,
    request: Request,
    session: DbSession,
    redis: RedisClient,
    settings: SettingsDep,
) -> TokenPairOut:
    return await identity_service.verify_otp(
        session,
        redis,
        settings,
        payload,
        client_ip=_client_ip(request),
    )


@router.post("/auth/refresh", response_model=TokenPairOut)
async def auth_refresh(
    payload: RefreshIn,
    session: DbSession,
    settings: SettingsDep,
) -> TokenPairOut:
    return await identity_service.refresh_tokens(session, settings, payload.refresh_token)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def auth_logout(payload: LogoutIn, session: DbSession) -> Response:
    await identity_service.logout(session, payload.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=MeOut)
async def get_me(session: DbSession, user_id: CurrentUserId) -> MeOut:
    return await identity_service.get_me(session, user_id)


@router.patch("/me", response_model=MeOut)
async def patch_me(
    payload: MePatchIn,
    session: DbSession,
    user_id: CurrentUserId,
) -> MeOut:
    return await identity_service.patch_me(session, user_id, payload)


@router.post("/me/phone/otp/request", status_code=status.HTTP_204_NO_CONTENT)
async def phone_change_request(
    payload: PhoneChangeRequestIn,
    request: Request,
    session: DbSession,
    redis: RedisClient,
    user_id: CurrentUserId,
) -> Response:
    await identity_service.request_phone_change(
        session,
        redis,
        user_id,
        payload,
        client_ip=_client_ip(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/me/phone/otp/verify", response_model=MeOut)
async def phone_change_verify(
    payload: PhoneChangeVerifyIn,
    request: Request,
    session: DbSession,
    redis: RedisClient,
    settings: SettingsDep,
    user_id: CurrentUserId,
) -> MeOut:
    return await identity_service.verify_phone_change(
        session,
        redis,
        settings,
        user_id,
        payload,
        client_ip=_client_ip(request),
    )


@router.post("/me/avatar", response_model=MeOut)
async def upload_avatar(
    session: DbSession,
    user_id: CurrentUserId,
    file: Annotated[UploadFile, File()],
) -> MeOut:
    url = await identity_media.save_profile_image(file, user_id=user_id, kind="avatar")
    return await identity_service.set_user_media_url(session, user_id, kind="avatar", url=url)


@router.post("/me/cover", response_model=MeOut)
async def upload_cover(
    session: DbSession,
    user_id: CurrentUserId,
    file: Annotated[UploadFile, File()],
) -> MeOut:
    url = await identity_media.save_profile_image(file, user_id=user_id, kind="cover")
    return await identity_service.set_user_media_url(session, user_id, kind="cover", url=url)

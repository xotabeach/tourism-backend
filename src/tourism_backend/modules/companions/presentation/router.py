"""Groups of companions (spec 11b): create, invite, answer, code, leave."""

from fastapi import APIRouter, Response, status

from tourism_backend.api.deps import CurrentUserId, DbSession, RedisClient, SettingsDep
from tourism_backend.modules.companions.application import groups, progress, start
from tourism_backend.modules.companions.application.schemas import (
    CompanionGroupCreateIn,
    CompanionGroupListOut,
    CompanionGroupOut,
    CompanionInviteIn,
    CompanionJoinIn,
    CompanionProgressOut,
    CompanionReadyIn,
    CompanionReportIn,
)

router = APIRouter(tags=["companions"])


@router.post(
    "/companion-groups", response_model=CompanionGroupOut, status_code=status.HTTP_201_CREATED
)
async def create_group(
    payload: CompanionGroupCreateIn, session: DbSession, user_id: CurrentUserId
) -> CompanionGroupOut:
    return await groups.create_group(session, user_id=user_id, route_id=payload.route_id)


@router.get("/me/companion-groups", response_model=CompanionGroupListOut)
async def list_my_groups(session: DbSession, user_id: CurrentUserId) -> CompanionGroupListOut:
    return await groups.list_my_groups(session, user_id=user_id)


@router.post("/companion-groups/join", response_model=CompanionGroupOut)
async def join_by_code(
    payload: CompanionJoinIn,
    session: DbSession,
    redis: RedisClient,
    settings: SettingsDep,
    user_id: CurrentUserId,
) -> CompanionGroupOut:
    return await groups.join_by_code(
        session, redis, settings=settings, user_id=user_id, code=payload.code
    )


@router.get("/companion-groups/{group_id}", response_model=CompanionGroupOut)
async def get_group(
    group_id: str, session: DbSession, settings: SettingsDep, user_id: CurrentUserId
) -> CompanionGroupOut:
    return await groups.get_group(session, settings=settings, group_id=group_id, user_id=user_id)


@router.post("/companion-groups/{group_id}/invites", response_model=CompanionGroupOut)
async def invite(
    group_id: str,
    payload: CompanionInviteIn,
    session: DbSession,
    settings: SettingsDep,
    user_id: CurrentUserId,
) -> CompanionGroupOut:
    return await groups.invite(
        session, settings=settings, group_id=group_id, actor_id=user_id, invitee_id=payload.user_id
    )


@router.post("/companion-groups/{group_id}/accept", response_model=CompanionGroupOut)
async def accept(
    group_id: str, session: DbSession, settings: SettingsDep, user_id: CurrentUserId
) -> CompanionGroupOut:
    return await groups.respond(
        session, settings=settings, group_id=group_id, user_id=user_id, accept=True
    )


@router.post("/companion-groups/{group_id}/decline", response_model=CompanionGroupOut)
async def decline(
    group_id: str, session: DbSession, settings: SettingsDep, user_id: CurrentUserId
) -> CompanionGroupOut:
    return await groups.respond(
        session, settings=settings, group_id=group_id, user_id=user_id, accept=False
    )


@router.post(
    "/companion-groups/{group_id}/requests/{member_user_id}/approve",
    response_model=CompanionGroupOut,
)
async def approve_request(
    group_id: str, member_user_id: str, session: DbSession, user_id: CurrentUserId
) -> CompanionGroupOut:
    return await groups.answer_request(
        session, group_id=group_id, actor_id=user_id, user_id=member_user_id, approve=True
    )


@router.post(
    "/companion-groups/{group_id}/requests/{member_user_id}/reject",
    response_model=CompanionGroupOut,
)
async def reject_request(
    group_id: str, member_user_id: str, session: DbSession, user_id: CurrentUserId
) -> CompanionGroupOut:
    return await groups.answer_request(
        session, group_id=group_id, actor_id=user_id, user_id=member_user_id, approve=False
    )


@router.delete("/companion-groups/{group_id}/members/{member_user_id}")
async def remove_member(
    group_id: str,
    member_user_id: str,
    session: DbSession,
    settings: SettingsDep,
    user_id: CurrentUserId,
) -> Response:
    """The organizer removes someone; a member removes themselves to leave."""
    result = await groups.remove_member(
        session, settings=settings, group_id=group_id, actor_id=user_id, user_id=member_user_id
    )
    if result is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return Response(content=result.model_dump_json(), media_type="application/json")


@router.post("/companion-groups/{group_id}/cancel", response_model=CompanionGroupOut)
async def cancel_group(
    group_id: str, session: DbSession, settings: SettingsDep, user_id: CurrentUserId
) -> CompanionGroupOut:
    return await groups.cancel_group(
        session, settings=settings, group_id=group_id, actor_id=user_id
    )


@router.post("/companion-groups/{group_id}/report", status_code=status.HTTP_204_NO_CONTENT)
async def report_invitation(
    group_id: str, payload: CompanionReportIn, session: DbSession, user_id: CurrentUserId
) -> None:
    await groups.report_invitation(session, group_id=group_id, user_id=user_id, payload=payload)


@router.post("/companion-groups/{group_id}/ready", response_model=CompanionGroupOut)
async def set_ready(
    group_id: str, payload: CompanionReadyIn, session: DbSession, user_id: CurrentUserId
) -> CompanionGroupOut:
    """«Готов» on the gathering screen; refused while another run is open."""
    return await start.set_ready(session, group_id=group_id, user_id=user_id, ready=payload.ready)


@router.post("/companion-groups/{group_id}/start", response_model=CompanionGroupOut)
async def start_group(
    group_id: str, session: DbSession, settings: SettingsDep, user_id: CurrentUserId
) -> CompanionGroupOut:
    """The organizer's start; each phone then starts its own run."""
    return await start.start_group(session, settings=settings, group_id=group_id, actor_id=user_id)


@router.get("/companion-groups/{group_id}/progress", response_model=CompanionProgressOut)
async def group_progress(
    group_id: str, session: DbSession, settings: SettingsDep, user_id: CurrentUserId
) -> CompanionProgressOut:
    """Who is at which stop, the result and «кто первым дошёл»; the app polls it
    about every half a minute while the group screen is open (D6)."""
    return await progress.group_progress(
        session, settings=settings, group_id=group_id, user_id=user_id
    )

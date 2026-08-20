"""Travel+ HTTP API (mock checkout + cancel; no store billing)."""

from fastapi import APIRouter

from tourism_backend.api.deps import CurrentUserId, DbSession
from tourism_backend.modules.identity.application import service as identity_service
from tourism_backend.modules.identity.application.schemas import MeOut
from tourism_backend.modules.subscriptions.application import service as travel_plus
from tourism_backend.modules.subscriptions.application.schemas import TravelPlusActivateIn

router = APIRouter(tags=["travel-plus"])


@router.post("/me/travel-plus/activate", response_model=MeOut)
async def activate_travel_plus(
    payload: TravelPlusActivateIn,
    session: DbSession,
    user_id: CurrentUserId,
) -> MeOut:
    await travel_plus.activate_travel_plus(
        session,
        user_id=user_id,
        plan=payload.plan,
        source="mock_checkout",
    )
    return await identity_service.get_me(session, user_id)


@router.post("/me/travel-plus/cancel", response_model=MeOut)
async def cancel_travel_plus(
    session: DbSession,
    user_id: CurrentUserId,
) -> MeOut:
    await travel_plus.cancel_travel_plus(session, user_id=user_id)
    return await identity_service.get_me(session, user_id)

from uuid import UUID

from fastapi import APIRouter, Response, status

from tourism_backend.api.deps import CurrentUserId, DbSession
from tourism_backend.modules.favorites.application import service as favorites_service
from tourism_backend.modules.favorites.application.schemas import FavoritesOut

router = APIRouter(tags=["favorites"])


@router.get("/favorites", response_model=FavoritesOut)
async def get_favorites(session: DbSession, user_id: CurrentUserId) -> FavoritesOut:
    return await favorites_service.list_favorites(session, user_id)


@router.put("/favorites/places/{place_id}", status_code=status.HTTP_204_NO_CONTENT)
async def put_favorite_place(
    place_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> Response:
    await favorites_service.add_favorite_place(session, user_id, place_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/favorites/places/{place_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_favorite_place(
    place_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> Response:
    await favorites_service.remove_favorite_place(session, user_id, place_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/favorites/routes/{route_id}", status_code=status.HTTP_204_NO_CONTENT)
async def put_favorite_route(
    route_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> Response:
    await favorites_service.add_favorite_route(session, user_id, route_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/favorites/routes/{route_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_favorite_route(
    route_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> Response:
    await favorites_service.remove_favorite_route(session, user_id, route_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

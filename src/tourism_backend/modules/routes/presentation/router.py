from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status

from tourism_backend.api.deps import CurrentUserId, DbSession
from tourism_backend.modules.routes.application import media as route_media
from tourism_backend.modules.routes.application import review_media, review_service
from tourism_backend.modules.routes.application import service as routes_service
from tourism_backend.modules.routes.application.review_schemas import (
    MyRouteReviewListOut,
    RouteReviewCreateIn,
    RouteReviewListOut,
    RouteReviewMediaOut,
    RouteReviewOut,
)
from tourism_backend.modules.routes.application.schemas import (
    RouteCatalogSort,
    RouteDetailOut,
    RouteListOut,
    RouteSource,
    UserRouteDraftIn,
    UserRouteDraftOut,
    UserRouteMediaOut,
)

router = APIRouter(tags=["routes"])


@router.post("/routes/drafts", response_model=UserRouteDraftOut)
async def save_route_draft(
    payload: UserRouteDraftIn,
    session: DbSession,
    user_id: CurrentUserId,
) -> UserRouteDraftOut:
    return await routes_service.save_user_route_draft(
        session,
        owner_user_id=user_id,
        payload=payload,
    )


@router.delete(
    "/routes/drafts/{route_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def discard_route_draft(
    route_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> Response:
    await routes_service.discard_user_route_draft(
        session,
        route_id=route_id,
        owner_user_id=user_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/routes/drafts/{route_id}/media",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def clear_route_draft_media(
    route_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> Response:
    await routes_service.clear_user_route_media(
        session,
        route_id=route_id,
        owner_user_id=user_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/routes/drafts/{route_id}/media",
    response_model=UserRouteMediaOut,
)
async def upload_route_draft_media(
    route_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
    file: Annotated[UploadFile, File()],
    position: Annotated[int, Form(ge=0, le=9)],
) -> UserRouteMediaOut:
    # Authorize before reading/writing an attacker-controlled upload.
    await routes_service.ensure_user_route_editable(
        session,
        route_id=route_id,
        owner_user_id=user_id,
    )
    saved = await route_media.save_route_media(file, route_id=route_id)
    return await routes_service.add_user_route_media(
        session,
        route_id=route_id,
        owner_user_id=user_id,
        position=position,
        saved=saved,
    )


@router.post("/routes/{route_id}/submit", response_model=UserRouteDraftOut)
async def submit_route(
    route_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> UserRouteDraftOut:
    return await routes_service.submit_user_route(
        session,
        route_id=route_id,
        owner_user_id=user_id,
    )


@router.get("/routes", response_model=RouteListOut)
async def get_routes(
    session: DbSession,
    region_slug: str | None = Query(default=None, max_length=128),
    transport_mode: str | None = Query(default=None, max_length=32),
    difficulty: str | None = Query(default=None, max_length=32),
    q: str | None = Query(default=None, max_length=200),
    source: RouteSource | None = None,
    sort: RouteCatalogSort = "default",
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
) -> RouteListOut:
    return await routes_service.list_routes(
        session,
        region_slug=region_slug,
        transport_mode=transport_mode,
        difficulty=difficulty,
        q=q,
        source=source,
        sort=sort,
        limit=limit,
        offset=offset,
    )


@router.get("/routes/mine", response_model=RouteListOut)
async def get_my_routes(
    session: DbSession,
    user_id: CurrentUserId,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
) -> RouteListOut:
    return await routes_service.list_routes_for_owner(
        session,
        owner_user_id=user_id,
        limit=limit,
        offset=offset,
    )


@router.get("/routes/mine/{route_id}", response_model=RouteDetailOut)
async def get_my_route(
    route_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> RouteDetailOut:
    return await routes_service.get_owned_route(
        session,
        route_id=route_id,
        owner_user_id=user_id,
    )


@router.get("/routes/{route_id}", response_model=RouteDetailOut)
async def get_route(session: DbSession, route_id: UUID) -> RouteDetailOut:
    return await routes_service.get_route(session, route_id)


@router.get("/routes/{route_id}/reviews", response_model=RouteReviewListOut)
async def list_route_reviews(
    route_id: UUID,
    session: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
) -> RouteReviewListOut:
    return await review_service.list_published_reviews(
        session,
        route_id=route_id,
        limit=limit,
        offset=offset,
    )


@router.post("/routes/{route_id}/reviews", response_model=RouteReviewOut)
async def create_route_review(
    route_id: UUID,
    payload: RouteReviewCreateIn,
    session: DbSession,
    user_id: CurrentUserId,
) -> RouteReviewOut:
    return await review_service.upsert_review(
        session,
        route_id=route_id,
        author_user_id=user_id,
        payload=payload,
    )


@router.delete("/routes/{route_id}/reviews/{review_id}", status_code=204)
async def delete_route_review(
    route_id: UUID,
    review_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> None:
    await review_service.delete_own_review(
        session,
        route_id=route_id,
        review_id=review_id,
        author_user_id=user_id,
    )


@router.post(
    "/routes/{route_id}/reviews/{review_id}/media",
    response_model=RouteReviewMediaOut,
)
async def upload_route_review_image(
    route_id: UUID,
    review_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
    file: Annotated[UploadFile, File()],
    position: Annotated[int, Form(ge=0, le=5)],
) -> RouteReviewMediaOut:
    # Authorize before reading or persisting attacker-controlled bytes.
    await review_service.ensure_own_mutable_review_media(
        session,
        route_id=route_id,
        review_id=review_id,
        author_user_id=user_id,
    )
    saved = await review_media.save_review_image(file, review_id=review_id)
    try:
        return await review_service.add_review_image(
            session,
            route_id=route_id,
            review_id=review_id,
            author_user_id=user_id,
            position=position,
            saved=saved,
        )
    except Exception:
        review_media.delete_review_image(saved.storage_key, review_id=review_id)
        raise


@router.delete(
    "/routes/{route_id}/reviews/{review_id}/media/{media_id}",
    status_code=204,
)
async def delete_route_review_image(
    route_id: UUID,
    review_id: UUID,
    media_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> None:
    await review_service.delete_review_image_attachment(
        session,
        route_id=route_id,
        review_id=review_id,
        media_id=media_id,
        author_user_id=user_id,
    )


@router.get("/me/reviews", response_model=MyRouteReviewListOut)
async def list_my_reviews(
    session: DbSession,
    user_id: CurrentUserId,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
) -> MyRouteReviewListOut:
    return await review_service.list_my_reviews(
        session,
        author_user_id=user_id,
        limit=limit,
        offset=offset,
    )

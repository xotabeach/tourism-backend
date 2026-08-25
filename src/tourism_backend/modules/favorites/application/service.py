from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.favorites.application.schemas import FavoritesOut
from tourism_backend.modules.favorites.infrastructure.models import FavoritePlace, FavoriteRoute
from tourism_backend.modules.identity.application.travel_points import grant_due_travel_points
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.routes.infrastructure.models import Route

_PUBLIC_CATALOG_ROUTE = (
    Route.source.in_(("editorial", "user_created")),
    Route.visibility == "public",
    Route.lifecycle_status == "active",
    Route.publication_status == "published",
)


def is_catalog_favorite_route(route: Route) -> bool:
    """Same publication rules as the public catalog (M-3)."""
    return (
        route.source in {"editorial", "user_created"}
        and route.visibility == "public"
        and route.lifecycle_status == "active"
        and route.publication_status == "published"
    )


async def list_favorites(session: AsyncSession, user_id: UUID) -> FavoritesOut:
    places = await session.execute(
        select(FavoritePlace.place_id)
        .join(Place, Place.id == FavoritePlace.place_id)
        .where(
            FavoritePlace.user_id == user_id,
            Place.publication_status == "published",
        )
        .order_by(FavoritePlace.created_at.desc())
    )
    routes = await session.execute(
        select(FavoriteRoute.route_id)
        .join(Route, Route.id == FavoriteRoute.route_id)
        .where(FavoriteRoute.user_id == user_id, *_PUBLIC_CATALOG_ROUTE)
        .order_by(FavoriteRoute.created_at.desc())
    )
    return FavoritesOut(
        place_ids=[str(row[0]) for row in places.all()],
        route_ids=[str(row[0]) for row in routes.all()],
    )


async def add_favorite_place(session: AsyncSession, user_id: UUID, place_id: UUID) -> None:
    place = await session.get(Place, place_id)
    if place is None or place.publication_status != "published":
        raise AppError(code="not_found", message="Place not found", status_code=404)

    existing = await session.get(FavoritePlace, (user_id, place_id))
    if existing is not None:
        return
    session.add(FavoritePlace(user_id=user_id, place_id=place_id, created_at=datetime.now(UTC)))
    await session.commit()


async def remove_favorite_place(session: AsyncSession, user_id: UUID, place_id: UUID) -> None:
    existing = await session.get(FavoritePlace, (user_id, place_id))
    if existing is None:
        return
    await session.delete(existing)
    await session.commit()


async def add_favorite_route(session: AsyncSession, user_id: UUID, route_id: UUID) -> None:
    route = await session.get(Route, route_id)
    if route is None or not is_catalog_favorite_route(route):
        raise AppError(code="not_found", message="Route not found", status_code=404)

    existing = await session.get(FavoriteRoute, (user_id, route_id))
    if existing is not None:
        await grant_due_travel_points(session)
        return
    session.add(
        FavoriteRoute(
            user_id=user_id,
            route_id=route_id,
            created_at=datetime.now(UTC),
            author_points_awarded_at=None,
        )
    )
    await session.commit()
    await grant_due_travel_points(session)


async def remove_favorite_route(session: AsyncSession, user_id: UUID, route_id: UUID) -> None:
    existing = await session.get(FavoriteRoute, (user_id, route_id))
    if existing is None:
        return
    # Drop before award → no points; after award → points retained.
    await session.delete(existing)
    await session.commit()

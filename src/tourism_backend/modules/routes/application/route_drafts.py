"""An author's own route: drafts, days, sending to moderation and taking back."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from typing import cast as type_cast
from uuid import UUID, uuid4

from geoalchemy2 import Geometry, WKTElement
from geoalchemy2.functions import ST_X, ST_Y
from redis.asyncio import Redis
from sqlalchemy import (
    cast,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.route_builder.application.routing import (
    TransportMode,
)
from tourism_backend.modules.routes.application.difficulty import (
    lowest_manual_level,
)
from tourism_backend.modules.routes.application.route_catalog import (
    author_rejection_text,
)
from tourism_backend.modules.routes.application.route_detail import (
    _days_for_route,
)
from tourism_backend.modules.routes.application.route_preview import (
    CAR_TAG,
    _route_geometry_for_places,
)
from tourism_backend.modules.routes.application.schemas import (
    RouteDayOut,
    RoutePublicationStatus,
    UserRouteDraftIn,
    UserRouteDraftOut,
    UserRouteEditableOut,
    UserRouteEditablePlaceOut,
    UserRouteMediaOut,
)
from tourism_backend.modules.routes.application.seaside import is_seaside as stops_are_seaside
from tourism_backend.modules.routes.application.structure import refresh_route_structure
from tourism_backend.modules.routes.infrastructure.models import (
    Route,
    RouteStop,
)


async def set_user_route_days(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
    ends_after_stop_ids: Sequence[UUID],
) -> list[RouteDayOut]:
    """The author ends days after these stops («закончить день здесь», D7).

    The boundaries are kept by place, so they survive the author's next save
    of the stops; from now on the days are not recomputed (D8).
    """
    route = await _owned_editable_route(session, route_id=route_id, owner_user_id=owner_user_id)
    stops = (
        await session.execute(
            select(RouteStop.id, RouteStop.place_id)
            .where(RouteStop.route_id == route.id)
            .order_by(RouteStop.position)
        )
    ).all()
    order = {stop_id: index for index, (stop_id, _place) in enumerate(stops)}
    chosen = [order.get(stop_id) for stop_id in ends_after_stop_ids]
    if (
        any(index is None for index in chosen)
        or chosen != sorted(set(chosen))  # type: ignore[type-var]
        or (chosen and chosen[-1] == len(stops) - 1)
    ):
        raise AppError(
            code="invalid_day_breaks",
            message="Дни заканчиваются после точек маршрута по порядку, кроме последней",
            status_code=422,
        )
    route.day_breaks = [str(stops[index][1]) for index in chosen if index is not None]
    route.days_manual = True
    route.updated_at = datetime.now(UTC)
    await refresh_route_structure(session, route)
    await session.commit()
    return await _days_for_route(session, route.id)


async def reset_user_route_days(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
) -> list[RouteDayOut]:
    """«Разделить заново»: back to the days the route's norms give (D8)."""
    route = await _owned_editable_route(session, route_id=route_id, owner_user_id=owner_user_id)
    route.day_breaks = None
    route.days_manual = False
    route.updated_at = datetime.now(UTC)
    await refresh_route_structure(session, route)
    await session.commit()
    return await _days_for_route(session, route.id)


# A published route is editable, but saving sends it back through review —
# same rule as articles: otherwise moderation is bypassed by publishing
# something plain and swapping the stops afterwards. `pending_review` is
# editable in place; nothing has been approved yet.
_EDITABLE_ROUTE_STATUSES = frozenset({"draft", "rejected", "pending_review", "published"})


def _take_manual_difficulty(
    route: Route, payload: UserRouteDraftIn, *, shown_before: int | None
) -> None:
    """The author's rating from an editor save (spec 17, D9, D15)."""
    if payload.difficulty_manual is True:
        route.difficulty_manual = payload.difficulty
        route.difficulty_manual_by = "author"
    elif payload.difficulty_manual is False:
        if route.difficulty_manual_by != "editorial":
            route.difficulty_manual = None
            route.difficulty_manual_by = None
    elif route.difficulty_manual_by == "author" and payload.difficulty != shown_before:
        # An older app: it always sends a number, a changed one is the
        # author's new rating. On «Авто» its default 3 means nothing.
        route.difficulty_manual = payload.difficulty


async def _owned_editable_route(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
    allowed: frozenset[str] = _EDITABLE_ROUTE_STATUSES,
) -> Route:
    route = await session.get(Route, route_id)
    if (
        route is None
        or route.owner_user_id != owner_user_id
        or route.source not in {"user_created", "generated"}
    ):
        raise AppError(code="route_not_found", message="Route not found", status_code=404)
    if route.publication_status not in allowed:
        raise AppError(
            code="route_not_editable",
            message="Route cannot be edited in its current status",
            status_code=409,
        )
    return route


async def get_user_route_for_edit(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
) -> UserRouteEditableOut:
    """The author's own route, in the shape the editor needs to resume.

    Pace, filters and difficulty are stored inside `accessibility` and are
    not part of the public payload, so the editor could previously only be
    resumed from the device that still held the local draft.
    """
    route = await _owned_editable_route(
        session,
        route_id=route_id,
        owner_user_id=owner_user_id,
    )
    # Places come back with their card data, so the editor can redraw the
    # stop list without a request per place.
    geom = cast(Place.location, Geometry)
    stop_rows = (
        await session.execute(
            select(Place, ST_X(geom), ST_Y(geom))
            .join(RouteStop, RouteStop.place_id == Place.id)
            .where(RouteStop.route_id == route.id)
            .order_by(RouteStop.position)
        )
    ).all()
    accessibility = route.accessibility if isinstance(route.accessibility, dict) else {}
    raw_filters = accessibility.get("filters")
    filters = (
        [item for item in raw_filters if isinstance(item, str)]
        if isinstance(raw_filters, list)
        else []
    )
    pace = accessibility.get("travel_pace")
    media = list(
        (
            await session.scalars(
                select(MediaAttachment)
                .where(
                    MediaAttachment.entity_type == "route",
                    MediaAttachment.entity_id == route.id,
                    MediaAttachment.status == "active",
                )
                .order_by(MediaAttachment.sort_order)
            )
        ).all()
    )
    return UserRouteEditableOut(
        id=route.id,
        publication_status=route.publication_status,  # type: ignore[arg-type]
        name=route.name,
        description=route.description or "",
        places=[
            UserRouteEditablePlaceOut(
                id=place.id,
                name=place.name,
                subtitle=place.short_description or "",
                lat=lat,
                lng=lng,
            )
            for place, lng, lat in stop_rows
        ],
        filters=filters,
        pace=pace if pace in {"calm", "moderate", "active"} else "calm",
        # The shown level: what an older editor sends back unchanged (D15).
        difficulty=route.difficulty_level or 3,
        difficulty_manual=route.difficulty_manual is not None
        and route.difficulty_manual_by == "author",
        difficulty_auto=route.difficulty_auto,
        difficulty_breakdown=(
            accessibility.get("difficulty")
            if isinstance(accessibility.get("difficulty"), dict)
            else None
        ),
        rejection_reason=author_rejection_text(route),
        media=[
            UserRouteMediaOut(
                id=item.id,
                public_path=item.public_path,
                kind="video" if str(item.content_type or "").startswith("video/") else "image",
                position=item.sort_order,
            )
            for item in media
        ],
        updated_at=route.updated_at,
        day_breaks=[UUID(value) for value in route.day_breaks or [] if _is_uuid(value)],
    )


def _is_uuid(value: object) -> bool:
    try:
        UUID(str(value))
    except ValueError:
        return False
    return True


_DRAFT_CLOCK_TOLERANCE = timedelta(milliseconds=5)


def _reject_stale_draft_save(route: Route, expected_updated_at: datetime | None) -> None:
    """Refuse to overwrite a draft that changed after the caller last saw it.

    Compared with a few milliseconds of slack: the client echoes the timestamp
    the server sent, and it may lose sub-millisecond digits on the way.
    """

    if expected_updated_at is None:
        return
    seen = (
        expected_updated_at
        if expected_updated_at.tzinfo is not None
        else expected_updated_at.replace(tzinfo=UTC)
    )
    if route.updated_at > seen + _DRAFT_CLOCK_TOLERANCE:
        raise AppError(
            code="draft_conflict",
            message="The draft was changed on another device",
            status_code=409,
            details={"updated_at": route.updated_at.isoformat()},
        )


async def save_user_route_draft(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    payload: UserRouteDraftIn,
    redis: Redis | None = None,
) -> UserRouteDraftOut:
    places = list(
        (
            await session.scalars(
                select(Place).where(
                    Place.id.in_(payload.place_ids),
                    Place.publication_status == "published",
                )
            )
        ).all()
    )
    place_by_id = {place.id: place for place in places}
    if len(place_by_id) != len(payload.place_ids):
        raise AppError(
            code="invalid_route_place",
            message="One or more route places are unavailable",
            status_code=400,
        )
    region_ids = {place.region_id for place in places}
    if len(region_ids) != 1:
        raise AppError(
            code="invalid_route_region",
            message="All route places must belong to one region",
            status_code=400,
        )

    now = datetime.now(UTC)
    route_key = payload.route_id
    if route_key is None and payload.client_draft_id is not None:
        # A retry after a lost response: reuse the draft this key already made.
        route_key = await session.scalar(
            select(Route.id).where(
                Route.owner_user_id == owner_user_id,
                Route.client_draft_id == payload.client_draft_id,
                Route.publication_status != "deleted",
            )
        )
    if route_key is None:
        route_id = uuid4()
        route = Route(
            client_draft_id=payload.client_draft_id,
            id=route_id,
            region_id=next(iter(region_ids)),
            owner_user_id=owner_user_id,
            name=payload.name,
            slug=f"user-{route_id.hex}",
            source="user_created",
            visibility="private",
            lifecycle_status="draft",
            publication_status="draft",
            freshness_status="unknown",
            created_at=now,
            updated_at=now,
        )
        session.add(route)
        try:
            await session.flush()
        except IntegrityError as exc:
            # Two saves with the same key raced; the other one wins and the
            # caller retries, finding the draft it created.
            await session.rollback()
            raise AppError(
                code="draft_busy",
                message="The draft is being saved, try again",
                status_code=409,
            ) from exc
        previous_status = "draft"
    else:
        route = await _owned_editable_route(
            session,
            route_id=route_key,
            owner_user_id=owner_user_id,
        )
        _reject_stale_draft_save(route, payload.expected_updated_at)
        previous_status = route.publication_status
        await session.execute(delete(RouteStop).where(RouteStop.route_id == route.id))

    route.region_id = next(iter(region_ids))
    route.name = payload.name
    route.short_description = payload.description[:240] or None
    route.description = payload.description or None
    route.visibility = "private"
    route.lifecycle_status = "draft"
    # A route that had already been through review goes back into the queue
    # rather than silently to "draft": the author edited something live, and
    # it must not reappear in the catalogue until it is checked again.
    route.publication_status = (
        "pending_review" if previous_status in {"pending_review", "published"} else "draft"
    )
    shown_before = route.difficulty_level
    _take_manual_difficulty(route, payload, shown_before=shown_before)
    # The author's «На машине» tag drives the route, with walks to what a car
    # cannot reach (spec 14b); anything else is walked, as before.
    driven = CAR_TAG in payload.filters
    route_mode: TransportMode = "car" if driven else "walk"
    route.transport_mode = "car" if driven else "walking"
    route.suitable_for_children = "С детьми" in payload.filters
    if payload.day_breaks is not None:
        # Empty is «split by the norms again»; kept by place (spec 14a).
        route.day_breaks = [str(place_id) for place_id in payload.day_breaks] or None
        route.days_manual = bool(payload.day_breaks)
    accessibility: dict[str, Any] = {
        "travel_pace": payload.pace,
        "filters": payload.filters,
    }
    # Road geometry is computed once and read from the database ever after:
    # opening a route redraws it without spending a routing call, and the
    # static map endpoint needs it to draw anything but straight lines.
    #
    # Only when the points actually changed, though. Renaming a route or
    # fixing a typo in its description used to wait on an external routing
    # call for a line that was already correct, which is most of what made
    # saving feel slow (reported 2026-09-08).
    stops_key = [str(place_id) for place_id in payload.place_ids]
    previous_routing = (
        route.accessibility.get("routing") if isinstance(route.accessibility, dict) else None
    )
    unchanged = (
        isinstance(previous_routing, dict)
        and previous_routing.get("place_ids") == stops_key
        and previous_routing.get("transport_mode", "walk") == route_mode
        and route.geometry is not None
    )
    if isinstance(route.accessibility, dict) and "terrain" in route.accessibility:
        # The ground fetched for these stops; the terrain job fetches it
        # again when they change (spec 17, D20).
        accessibility["terrain"] = route.accessibility["terrain"]
    if unchanged:
        accessibility["routing"] = previous_routing
    else:
        # The «Море» tag follows the stops (BACKEND-19); an editor's fix in
        # the admin holds until the author changes the stops again.
        route.is_seaside = await stops_are_seaside(session, payload.place_ids)
        routed = await _route_geometry_for_places(
            session,
            places=places,
            place_ids=payload.place_ids,
            redis=redis,
            transport_mode=route_mode,
        )
        if routed is not None:
            geometry_wkt, routing_meta = routed
            route.geometry = WKTElement(geometry_wkt, srid=4326)
            accessibility["routing"] = {
                **routing_meta,
                "place_ids": stops_key,
                "transport_mode": route_mode,
            }
    route.accessibility = accessibility
    route.updated_at = now

    for position, place_id in enumerate(payload.place_ids, start=1):
        session.add(
            RouteStop(
                id=uuid4(),
                route_id=route.id,
                place_id=place_id,
                position=position,
                is_optional=False,
                created_at=now,
                updated_at=now,
            )
        )
    await session.flush()
    # The stops are new rows: rebuild days and segments now, keeping the
    # author's day boundaries by place (spec 14a).
    await refresh_route_structure(session, route)
    if (
        payload.difficulty_manual is True
        and route.difficulty_manual is not None
        and route.difficulty_auto is not None
        and route.difficulty_manual < lowest_manual_level(route.difficulty_auto)
    ):
        # Nothing is saved: the author sees the estimate and why (D9).
        raise AppError(
            code="difficulty_below_estimate",
            message=(
                f"Сложность не может быть ниже {lowest_manual_level(route.difficulty_auto)}: "
                f"по расчёту маршрут {route.difficulty_auto} из 5"
            ),
            status_code=422,
            details={
                "estimate": route.difficulty_auto,
                "lowest": lowest_manual_level(route.difficulty_auto),
                "breakdown": (route.accessibility or {}).get("difficulty"),
            },
        )
    await session.commit()
    await session.refresh(route)
    return UserRouteDraftOut(
        id=route.id,
        publication_status=type_cast(RoutePublicationStatus, route.publication_status),
        updated_at=route.updated_at,
    )


async def submit_user_route(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
) -> UserRouteDraftOut:
    # Narrower than editing: a route already queued or already live has
    # nothing to submit — editing a published one re-queues it by itself.
    route = await _owned_editable_route(
        session,
        route_id=route_id,
        owner_user_id=owner_user_id,
        allowed=frozenset({"draft", "rejected"}),
    )
    media_count = int(
        await session.scalar(
            select(func.count()).where(
                MediaAttachment.entity_type == "route",
                MediaAttachment.entity_id == route.id,
                MediaAttachment.status == "active",
            )
        )
        or 0
    )
    stops_count = int(
        await session.scalar(select(func.count()).where(RouteStop.route_id == route.id)) or 0
    )
    if media_count == 0:
        raise AppError(
            code="route_media_required",
            message="At least one route photo or video is required",
            status_code=400,
        )
    if stops_count < 2:
        raise AppError(
            code="route_points_required",
            message="Start and finish are required",
            status_code=400,
        )
    route.publication_status = "pending_review"
    # The author answered the moderator's note by sending the route again.
    route.rejection_reason = None
    route.moderator_note = None
    route.visibility = "private"
    route.lifecycle_status = "draft"
    route.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(route)
    return UserRouteDraftOut(
        id=route.id,
        publication_status=type_cast(RoutePublicationStatus, route.publication_status),
        updated_at=route.updated_at,
    )


async def discard_user_route_draft(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
) -> None:
    route = await _owned_editable_route(
        session,
        route_id=route_id,
        owner_user_id=owner_user_id,
    )
    now = datetime.now(UTC)
    route.publication_status = "deleted"
    route.lifecycle_status = "archived"
    route.visibility = "private"
    route.updated_at = now
    await session.execute(
        update(MediaAttachment)
        .where(
            MediaAttachment.entity_type == "route",
            MediaAttachment.entity_id == route_id,
            MediaAttachment.status == "active",
        )
        .values(status="archived", updated_at=now)
    )
    await session.commit()


async def withdraw_user_route(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
) -> UserRouteDraftOut:
    route = await session.get(Route, route_id)
    if (
        route is None
        or route.owner_user_id != owner_user_id
        or route.source not in {"user_created", "generated"}
    ):
        raise AppError(code="route_not_found", message="Route not found", status_code=404)
    if route.publication_status not in {"pending_review", "published"}:
        raise AppError(
            code="route_not_withdrawable",
            message="Route cannot be withdrawn in its current status",
            status_code=409,
        )
    route.publication_status = "draft"
    route.visibility = "private"
    route.lifecycle_status = "draft"
    route.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(route)
    return UserRouteDraftOut(
        id=route.id,
        publication_status=type_cast(RoutePublicationStatus, route.publication_status),
        updated_at=route.updated_at,
    )

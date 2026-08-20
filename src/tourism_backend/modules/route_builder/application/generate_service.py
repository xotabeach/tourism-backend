"""Generate personal route draft / chat proposal with quotas."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from geoalchemy2 import Geometry, WKTElement
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.route_builder.application.place_picker import (
    PickedPlace,
    pick_places_for_params,
)
from tourism_backend.modules.route_builder.application.quota import (
    quota_snapshot,
    require_generation_quota,
)
from tourism_backend.modules.route_builder.application.routing import (
    RouteWaypoint,
    RoutingError,
    RoutingResult,
    TransportMode,
)
from tourism_backend.modules.route_builder.application.schemas import (
    ActionsBlockOut,
    PlaceChipBlockOut,
    QuotaSnapshotOut,
    RouteGenerateIn,
    RouteGenerateOut,
    RouteMatchParamsIn,
    RouteProposalCardBlockOut,
    RouteProposalOut,
)
from tourism_backend.modules.route_builder.infrastructure.models import (
    RouteGenerationEvent,
    RouteProposal,
)
from tourism_backend.modules.route_builder.infrastructure.routing_factory import (
    get_routing_provider,
)
from tourism_backend.modules.routes.infrastructure.models import Route, RouteStop
from tourism_backend.modules.subscriptions.application import service as travel_plus
from tourism_backend.modules.subscriptions.application.entitlements import (
    FREE_POLICY,
    policy_for_user,
    require_ai_chat,
)

_TRANSPORT_MAP = {
    "walk": "walking",
    "car": "car",
    "public": "public_transport",
    "mixed": "mixed",
}


def _sanitize_params(params: RouteMatchParamsIn, *, advanced: bool) -> RouteMatchParamsIn:
    if advanced:
        return params
    return params.model_copy(
        update={
            "budget_amount": None,
            "day_kind": "any",
            "paid_ok": None,
            "avoid_crowds": None,
        }
    )


def _difficulty_for_pace(pace: str) -> str:
    return {
        "calm": "easy",
        "moderate": "moderate",
        "active": "hard",
    }.get(pace, "moderate")


def _estimate_duration(
    places: list[PickedPlace],
    routing: RoutingResult | None = None,
) -> int:
    total = 0
    for place in places:
        total += place.recommended_visit_minutes or 45
    if routing is not None:
        total += max(0, routing.total_duration_seconds // 60)
    else:
        # Rough transit padding when routing was skipped (should be rare).
        total += max(0, len(places) - 1) * 25
    return total


def _transport_mode(params: RouteMatchParamsIn) -> TransportMode:
    mode = params.transport_mode or "walk"
    if mode in {"walk", "car", "public", "mixed"}:
        return mode
    return "walk"


def _line_wkt(waypoints: list[RouteWaypoint]) -> str:
    coords = ", ".join(f"{point.lng} {point.lat}" for point in waypoints)
    return f"LINESTRING({coords})"


async def _waypoints_for_places(
    session: AsyncSession,
    places: list[PickedPlace],
) -> list[RouteWaypoint]:
    place_ids = [place.place_id for place in places]
    geom = cast(Place.location, Geometry)
    rows = (
        await session.execute(
            select(Place.id, ST_X(geom), ST_Y(geom)).where(Place.id.in_(place_ids))
        )
    ).all()
    by_id = {
        place_id: (float(lng), float(lat))
        for place_id, lng, lat in rows
        if lng is not None and lat is not None
    }
    waypoints: list[RouteWaypoint] = []
    for place in places:
        coords = by_id.get(place.place_id)
        if coords is None:
            raise AppError(
                code="invalid_route_place",
                message=f"Place {place.place_id} has no coordinates",
                status_code=422,
            )
        waypoints.append(
            RouteWaypoint(
                lng=coords[0],
                lat=coords[1],
                place_id=place.place_id,
                label=place.name or None,
            )
        )
    return waypoints


async def _route_places(
    session: AsyncSession,
    *,
    places: list[PickedPlace],
    params: RouteMatchParamsIn,
) -> RoutingResult:
    waypoints = await _waypoints_for_places(session, places)
    provider = get_routing_provider()
    try:
        return await provider.route(
            waypoints=waypoints,
            transport_mode=_transport_mode(params),
        )
    except RoutingError as exc:
        raise AppError(code=exc.code, message=exc.message, status_code=422) from exc


def _title_for(params: RouteMatchParamsIn) -> str:
    interests = ", ".join(params.interests[:2]) if params.interests else "места"
    return f"{params.city} · {interests}"


def _assistant_text(params: RouteMatchParamsIn, places: list[PickedPlace]) -> str:
    names = ", ".join(place.name for place in places[:3])
    more = "" if len(places) <= 3 else f" и ещё {len(places) - 3}"
    return (
        f"Собрал черновик маршрута из {params.city}: {names}{more}. "
        "Можно создать маршрут, сохранить в черновик или уточнить параметры."
    )


def _blocks_for_proposal(
    proposal: RouteProposal,
    places: list[PickedPlace],
) -> list[PlaceChipBlockOut | RouteProposalCardBlockOut | ActionsBlockOut]:
    chips = [
        PlaceChipBlockOut(
            place_id=str(place.place_id),
            title=place.name,
            subtitle=place.short_description,
            duration_minutes=place.recommended_visit_minutes,
        )
        for place in places
    ]
    card = RouteProposalCardBlockOut(
        proposal_id=str(proposal.id),
        title=proposal.title,
        stops_count=len(places),
        duration_minutes=proposal.duration_minutes,
        cover_url=proposal.cover_url,
        place_ids=[str(place.place_id) for place in places],
    )
    actions = ActionsBlockOut(
        actions=[
            {"id": "accept_proposal", "label": "Создать маршрут"},
            {"id": "save_draft", "label": "В черновик"},
            {"id": "refine", "label": "Уточнить"},
            {"id": "reject", "label": "Другой вариант"},
        ]
    )
    return [*chips, card, actions]


def _proposal_out(
    proposal: RouteProposal,
    places: list[PickedPlace],
    quota: QuotaSnapshotOut,
) -> RouteProposalOut:
    return RouteProposalOut(
        proposal_id=str(proposal.id),
        status=proposal.status,  # type: ignore[arg-type]
        channel=proposal.channel,  # type: ignore[arg-type]
        title=proposal.title,
        assistant_text=proposal.assistant_text,
        place_ids=[str(pid) for pid in proposal.place_ids],
        duration_minutes=proposal.duration_minutes,
        cover_url=proposal.cover_url,
        route_id=str(proposal.route_id) if proposal.route_id else None,
        blocks=_blocks_for_proposal(proposal, places),
        quota=quota,
    )


async def _persist_generated_route(
    session: AsyncSession,
    *,
    user_id: UUID,
    params: RouteMatchParamsIn,
    places: list[PickedPlace],
    title: str,
    routing: RoutingResult,
) -> Route:
    now = datetime.now(UTC)
    route_id = uuid4()

    first = await session.get(Place, places[0].place_id)
    if first is None:
        raise AppError(code="invalid_route_place", message="Place missing", status_code=400)
    region_id = first.region_id

    description_bits = [
        f"Сгенерировано по параметрам: {params.city}",
        f"темп {params.pace}",
        f"длительность {params.duration}",
    ]
    if params.interests:
        description_bits.append("интересы: " + ", ".join(params.interests))
    description = "; ".join(description_bits)

    waypoints = await _waypoints_for_places(session, places)
    accessibility = {
        "travel_pace": params.pace,
        "day_kind": params.day_kind,
        "budget_amount": params.budget_amount,
        "generated": True,
        "routing": {
            "provider": routing.provider,
            "synthetic": routing.synthetic,
            "warnings": list(routing.warnings),
            "total_duration_seconds": routing.total_duration_seconds,
        },
    }

    route = Route(
        id=route_id,
        region_id=region_id,
        owner_user_id=user_id,
        name=title[:255],
        slug=f"generated-{route_id.hex}",
        short_description=description[:240],
        description=description,
        source="generated",
        visibility="private",
        lifecycle_status="draft",
        publication_status="draft",
        estimated_duration_minutes=_estimate_duration(places, routing),
        distance_meters=routing.total_distance_meters,
        difficulty=_difficulty_for_pace(params.pace),
        transport_mode=_TRANSPORT_MAP.get(params.transport_mode or "walk", "walking"),
        suitable_for_children=params.with_children,
        pets_allowed=params.with_pets,
        seasonality=[params.season] if params.season else None,
        accessibility=accessibility,
        geometry=WKTElement(_line_wkt(waypoints), srid=4326),
        freshness_status="unknown",
        created_at=now,
        updated_at=now,
    )
    session.add(route)
    await session.flush()
    for position, place in enumerate(places, start=1):
        session.add(
            RouteStop(
                id=uuid4(),
                route_id=route.id,
                place_id=place.place_id,
                position=position,
                visit_duration_minutes=place.recommended_visit_minutes,
                is_optional=False,
                created_at=now,
                updated_at=now,
            )
        )
    return route


async def generate_route(
    session: AsyncSession,
    *,
    user_id: UUID,
    payload: RouteGenerateIn,
) -> RouteGenerateOut:
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)
    await travel_plus.refresh_user_travel_plus(session, user=user)
    policy = policy_for_user(user)
    if payload.channel == "chat":
        require_ai_chat(user)

    params = _sanitize_params(payload.params, advanced=policy.advanced_filters_enabled)
    await require_generation_quota(session, user_id=user_id, policy=policy)

    places = await pick_places_for_params(
        session,
        params=params,
        max_points=policy.max_route_points,
    )
    routing = await _route_places(session, places=places, params=params)
    title = _title_for(params)
    assistant_text = _assistant_text(params, places)
    duration = _estimate_duration(places, routing)
    now = datetime.now(UTC)

    proposal = RouteProposal(
        id=uuid4(),
        user_id=user_id,
        channel=payload.channel,
        status="draft",
        title=title,
        assistant_text=assistant_text,
        params=params.model_dump(mode="json"),
        place_ids=[place.place_id for place in places],
        duration_minutes=duration,
        cover_url=None,
        route_id=None,
        created_at=now,
        updated_at=now,
    )
    session.add(proposal)
    await session.flush()

    route: Route | None = None
    persisted_draft = False
    if payload.channel == "form":
        route = await _persist_generated_route(
            session,
            user_id=user_id,
            params=params,
            places=places,
            title=title,
            routing=routing,
        )
        proposal.route_id = route.id
        proposal.status = "accepted"
        proposal.accepted_at = now
        proposal.updated_at = now
        persisted_draft = True

    event = RouteGenerationEvent(
        id=uuid4(),
        user_id=user_id,
        channel=payload.channel,
        proposal_id=proposal.id,
        route_id=route.id if route else None,
        created_at=now,
        updated_at=now,
    )
    session.add(event)
    await session.commit()
    await session.refresh(proposal)

    snap = await quota_snapshot(session, user_id=user_id, policy=policy)
    quota = snap
    proposal_out = _proposal_out(proposal, places, quota)
    return RouteGenerateOut(
        channel=payload.channel,
        proposal=proposal_out,
        route_id=str(route.id) if route else None,
        persisted_draft=persisted_draft,
    )


async def accept_proposal(
    session: AsyncSession,
    *,
    user_id: UUID,
    proposal_id: UUID,
) -> RouteProposalOut:
    proposal = await session.get(RouteProposal, proposal_id)
    if proposal is None or proposal.user_id != user_id:
        raise AppError(code="proposal_not_found", message="Proposal not found", status_code=404)
    if proposal.status == "accepted" and proposal.route_id is not None:
        user = await session.get(User, user_id)
        policy = policy_for_user(user) if user is not None else FREE_POLICY
        snap = await quota_snapshot(session, user_id=user_id, policy=policy)
        places = [
            PickedPlace(
                place_id=pid,
                name="",
                short_description=None,
                recommended_visit_minutes=None,
            )
            for pid in proposal.place_ids
        ]
        return _proposal_out(proposal, places, snap)
    if proposal.status not in {"draft", "rejected"}:
        raise AppError(
            code="proposal_not_acceptible",
            message="Proposal cannot be accepted",
            status_code=409,
        )

    params = RouteMatchParamsIn.model_validate(proposal.params)
    places = [
        PickedPlace(
            place_id=pid,
            name="",
            short_description=None,
            recommended_visit_minutes=None,
        )
        for pid in proposal.place_ids
    ]
    # Reload names for blocks
    from tourism_backend.modules.places.infrastructure.models import Place

    loaded: list[PickedPlace] = []
    for pid in proposal.place_ids:
        place = await session.get(Place, pid)
        if place is None:
            continue
        loaded.append(
            PickedPlace(
                place_id=place.id,
                name=place.name,
                short_description=place.short_description,
                recommended_visit_minutes=place.recommended_visit_minutes,
            )
        )
    if len(loaded) < 2:
        raise AppError(
            code="insufficient_places",
            message="Places for proposal are no longer available",
            status_code=422,
        )

    routing = await _route_places(session, places=loaded, params=params)
    route = await _persist_generated_route(
        session,
        user_id=user_id,
        params=params,
        places=loaded,
        title=proposal.title,
        routing=routing,
    )
    now = datetime.now(UTC)
    proposal.route_id = route.id
    proposal.status = "accepted"
    proposal.accepted_at = now
    proposal.updated_at = now
    await session.commit()
    await session.refresh(proposal)

    user = await session.get(User, user_id)
    policy = policy_for_user(user) if user is not None else FREE_POLICY
    snap = await quota_snapshot(session, user_id=user_id, policy=policy)
    return _proposal_out(proposal, loaded, snap)


async def reject_proposal(
    session: AsyncSession,
    *,
    user_id: UUID,
    proposal_id: UUID,
) -> RouteProposalOut:
    proposal = await session.get(RouteProposal, proposal_id)
    if proposal is None or proposal.user_id != user_id:
        raise AppError(code="proposal_not_found", message="Proposal not found", status_code=404)
    if proposal.status == "accepted":
        raise AppError(
            code="proposal_already_accepted",
            message="Accepted proposal cannot be rejected",
            status_code=409,
        )
    proposal.status = "rejected"
    proposal.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(proposal)

    from tourism_backend.modules.places.infrastructure.models import Place

    loaded: list[PickedPlace] = []
    for pid in proposal.place_ids:
        place = await session.get(Place, pid)
        if place is None:
            continue
        loaded.append(
            PickedPlace(
                place_id=place.id,
                name=place.name,
                short_description=place.short_description,
                recommended_visit_minutes=place.recommended_visit_minutes,
            )
        )
    user = await session.get(User, user_id)
    policy = policy_for_user(user) if user is not None else FREE_POLICY
    snap = await quota_snapshot(session, user_id=user_id, policy=policy)
    return _proposal_out(proposal, loaded, snap)

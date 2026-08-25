"""Allowlisted planning tools: LLM requests → backend → PostGIS → sanitized DATA."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import Select, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.geography.infrastructure.models import Locality, Region
from tourism_backend.modules.places.infrastructure.models import Place

ToolHandler = Callable[..., Awaitable[dict[str, Any]]]

_MAX_PLACES = 8
_MAX_TOOL_ROUNDS = 2
_ALLOWED_TOOLS = frozenset(
    {
        "search_places",
        "seasonal_recommendations",
        "get_place_details",
        "find_places_near_point",
    }
)

# Region-wide fallback when a city isn't pinned to a locality (e.g. «Крым»).
_REGION_CITY_ALIASES = frozenset({"крым", "crimea", "полуостров", "весь крым"})


def _looks_like_region_city(city: str) -> bool:
    return city.casefold().strip() in _REGION_CITY_ALIASES


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    name: str
    ok: bool
    data: dict[str, Any]


# Curated Crimea seasonal tips (SoT for recommendations until editorial CMS).
_SEASONAL_TIPS: dict[str, list[dict[str, str]]] = {
    "winter": [
        {
            "id": "winter_aipetri",
            "title": "Зимой — Ай-Петри",
            "body": (
                "Зимой в Крыму часто рекомендуют Ай-Петри: виды, прогулки, "
                "меньше пляжного шума. Можно стартовать из Ялты."
            ),
            "city_hint": "Ялта",
            "interest_hint": "горы",
            "accept_action": "accept_rec_winter_aipetri",
        },
        {
            "id": "winter_palace",
            "title": "Дворцы без жары",
            "body": (
                "В холодный сезон удобнее дворцовые маршруты ЮБК — меньше толп, "
                "комфортный темп для спокойной поездки."
            ),
            "city_hint": "Ялта",
            "interest_hint": "история",
            "accept_action": "accept_rec_winter_palace",
        },
    ],
    "spring": [
        {
            "id": "spring_sudak",
            "title": "Весна — Судак и Новый Свет",
            "body": (
                "Весной приятны прогулки у моря без пиковой жары: Судак, "
                "Новый Свет, тропы вдоль побережья."
            ),
            "city_hint": "Судак",
            "interest_hint": "море",
            "accept_action": "accept_rec_spring_sudak",
        }
    ],
    "summer": [
        {
            "id": "summer_foros",
            "title": "Летом — пляжи ЮБК",
            "body": (
                "Летом пляжный Крым особенно силён. К посещению часто советуют "
                "ЮБК и Форос — море + короткие прогулки."
            ),
            "city_hint": "Ялта",
            "interest_hint": "море",
            "accept_action": "accept_rec_summer_foros",
        },
        {
            "id": "summer_evpatoria",
            "title": "Лето с детьми — Евпатория",
            "body": (
                "Для семьи с детьми летом часто выбирают Евпаторию: пляжи, "
                "спокойный темп, короткие переезды."
            ),
            "city_hint": "Евпатория",
            "interest_hint": "море",
            "accept_action": "accept_rec_summer_kids",
        },
    ],
    "autumn": [
        {
            "id": "autumn_bahchisarai",
            "title": "Осень — Бахчисарай и пещеры",
            "body": (
                "Осенью комфортны маршруты вокруг Бахчисарая: история, виды, меньше летней духоты."
            ),
            "city_hint": "Бахчисарай",
            "interest_hint": "история",
            "accept_action": "accept_rec_autumn_bah",
        }
    ],
}


def season_from_month(month: int | None = None) -> str:
    m = month if month is not None else datetime.now(UTC).month
    if m in {12, 1, 2}:
        return "winter"
    if m in {3, 4, 5}:
        return "spring"
    if m in {6, 7, 8}:
        return "summer"
    return "autumn"


def parse_tool_calls(raw: object) -> list[ToolCall]:
    if not isinstance(raw, list):
        return []
    out: list[ToolCall] = []
    for item in raw[:8]:
        if len(out) >= _MAX_TOOL_ROUNDS:
            break
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or name not in _ALLOWED_TOOLS:
            continue
        args = item.get("arguments")
        if args is None:
            args = item.get("args")
        if not isinstance(args, dict):
            args = {}
        cleaned: dict[str, Any] = {}
        for key, value in list(args.items())[:8]:
            if not isinstance(key, str) or len(key) > 40:
                continue
            if isinstance(value, (str, int, float, bool)):
                if isinstance(value, str) and len(value) > 80:
                    cleaned[key] = value[:80]
                else:
                    cleaned[key] = value
        out.append(ToolCall(name=name, arguments=cleaned))
    return out


async def execute_tool(
    session: AsyncSession,
    call: ToolCall,
    *,
    constraints: dict[str, Any],
) -> ToolResult:
    try:
        if call.name == "search_places":
            data = await _search_places(session, call.arguments, constraints)
            return ToolResult(name=call.name, ok=True, data=data)
        if call.name == "get_place_details":
            data = await _get_place_details(session, call.arguments, constraints)
            return ToolResult(name=call.name, ok=True, data=data)
        if call.name == "find_places_near_point":
            data = await _find_places_near_point(session, call.arguments, constraints)
            return ToolResult(name=call.name, ok=True, data=data)
        if call.name == "seasonal_recommendations":
            data = _seasonal_recommendations(call.arguments, constraints)
            return ToolResult(name=call.name, ok=True, data=data)
    except Exception as exc:  # noqa: BLE001 — tools must not break the turn
        return ToolResult(
            name=call.name,
            ok=False,
            data={"error": "tool_failed", "detail": type(exc).__name__},
        )
    return ToolResult(name=call.name, ok=False, data={"error": "unknown_tool"})


async def prefetch_context(
    session: AsyncSession,
    *,
    constraints: dict[str, Any],
    confirmed_fields: list[str],
) -> dict[str, Any]:
    """Backend-first DATA for the model (no wait for tool_call)."""
    season = str(constraints.get("season") or season_from_month())
    tips = _seasonal_recommendations({"season": season}, constraints)
    places: list[dict[str, str]] = []
    if "city" in confirmed_fields or constraints.get("city"):
        places_payload = await _search_places(
            session,
            {
                "city": constraints.get("city"),
                "limit": 6,
            },
            constraints,
        )
        places = list(places_payload.get("places") or [])
    return {
        "season": season,
        "seasonal_recommendations": tips.get("items") or [],
        "place_candidates": places,
    }


def recommendation_accept_patch(action_id: str) -> dict[str, Any] | None:
    """Map accept_rec_* actions to constraint patches."""
    mapping: dict[str, dict[str, Any]] = {
        "accept_rec_winter_aipetri": {
            "city": "Ялта",
            "interests_add": ["горы"],
            "season": "winter",
            "pace": "calm",
        },
        "accept_rec_winter_palace": {
            "city": "Ялта",
            "interests_add": ["история"],
            "season": "winter",
            "pace": "calm",
        },
        "accept_rec_spring_sudak": {
            "city": "Судак",
            "interests_add": ["море"],
            "season": "spring",
        },
        "accept_rec_summer_foros": {
            "city": "Ялта",
            "interests_add": ["море"],
            "season": "summer",
            "pace": "calm",
        },
        "accept_rec_summer_kids": {
            "city": "Евпатория",
            "interests_add": ["море"],
            "season": "summer",
            "with_children": True,
            "pace": "calm",
        },
        "accept_rec_autumn_bah": {
            "city": "Бахчисарай",
            "interests_add": ["история"],
            "season": "autumn",
        },
    }
    return mapping.get(action_id)


async def _search_places(
    session: AsyncSession,
    args: dict[str, Any],
    constraints: dict[str, Any],
) -> dict[str, Any]:
    city = str(args.get("city") or constraints.get("city") or "").strip()
    if not city:
        return {"places": [], "note": "city_required"}
    limit = args.get("limit", 6)
    if not isinstance(limit, int):
        limit = 6
    limit = max(1, min(limit, _MAX_PLACES))
    region = await session.scalar(select(Region).where(Region.slug == "crimea"))
    if region is None:
        return {"places": []}

    region_wide = _looks_like_region_city(city)
    if not region_wide:
        locality_ids = list(
            await session.scalars(
                select(Locality.id).where(
                    Locality.region_id == region.id,
                    Locality.status == "active",
                    Locality.name.ilike(f"%{city}%"),
                )
            )
        )
    else:
        locality_ids = []

    stmt = select(Place).where(
        Place.region_id == region.id,
        Place.publication_status == "published",
        # Hard filters: never surface temporarily closed places as candidates.
        or_(
            Place.temporary_closure_status.is_(None),
            Place.temporary_closure_status == "none",
        ),
    )
    if locality_ids:
        stmt = stmt.where(
            or_(
                Place.locality_id.in_(locality_ids),
                Place.name.ilike(f"%{city}%"),
                Place.address.ilike(f"%{city}%"),
            )
        )
    elif not region_wide:
        stmt = stmt.where(
            or_(
                Place.name.ilike(f"%{city}%"),
                Place.address.ilike(f"%{city}%"),
            )
        )
    rows = (await session.scalars(stmt.limit(40))).all()
    interest = str(args.get("interest") or "").casefold()
    city_cf = city.casefold()
    scored: list[tuple[float, Place]] = []
    for place in rows:
        text = " ".join(
            part
            for part in (
                place.name,
                place.short_description or "",
                place.description or "",
            )
        ).casefold()
        score = 0.2
        if region_wide:
            # Region-wide query: every published place is a candidate; keep a
            # small locality baseline so ordering stays stable.
            score = 0.3
        elif city_cf in text or city_cf in (place.address or "").casefold():
            score += 0.4
        if interest and interest in text:
            score += 0.3
        scored.append((score, place))
    scored.sort(key=lambda item: (-item[0], item[1].name))
    places = [
        {
            "place_id": str(place.id),
            "title": place.name[:80],
            "subtitle": (place.short_description or "")[:120] or None,
        }
        for _, place in scored[:limit]
    ]
    return {"places": places, "city": city}


async def _get_place_details(
    session: AsyncSession,
    args: dict[str, Any],
    constraints: dict[str, Any],
) -> dict[str, Any]:
    """Structured facts for one place — the first 'fact' tool feeding RAG.

    Returns only allowlisted stable fields; hard facts (schedule, price) are
    read from PostGIS, never from the model. Anything else is deliberately
    excluded so the model can't re-invent coordinates or hours.
    """
    raw_id = args.get("place_id")
    if isinstance(raw_id, str) and raw_id.strip():
        place_id = raw_id.strip()
    else:
        first = list(constraints.get("place_candidates") or [])
        if not first or not isinstance(first[0], dict):
            return {"ok": False, "error": "place_id_required"}
        place_id = str(first[0].get("place_id") or "")
        if not place_id:
            return {"ok": False, "error": "place_id_required"}
    try:
        place = await session.get(Place, place_id)
    except Exception:  # noqa: BLE001 — invalid uuid must not break the turn
        return {"ok": False, "error": "place_not_found"}
    if place is None or place.publication_status != "published":
        return {"ok": False, "error": "place_not_found"}
    seasonality = list(place.seasonality or [])
    transport_access = list(place.access_transport or [])
    return {
        "ok": True,
        "place": {
            "place_id": str(place.id),
            "title": place.name[:120],
            "subtitle": (place.short_description or "")[:200] or None,
            "category": (place.category_name if hasattr(place, "category_name") else None),
            "is_paid": bool(place.is_paid),
            "price_notes": (place.price_notes or "")[:160] or None,
            "visit_minutes": place.recommended_visit_minutes,
            "children_ok": place.is_suitable_for_children,
            "pets_ok": place.is_suitable_for_pets,
            "access_transport": transport_access[:4],
            "seasonality": seasonality[:4],
            "crowding": (place.typical_crowding if hasattr(place, "typical_crowding") else None),
            "temporary_closure_status": place.temporary_closure_status,
        },
    }


async def _find_places_near_point(
    session: AsyncSession,
    args: dict[str, Any],
    constraints: dict[str, Any],
) -> dict[str, Any]:
    """Find published places near a lat/lng point (radius in meters).

    Distance filter and row cap run in PostGIS (``ST_DWithin`` + ``LIMIT``)
    so a growing catalog cannot load a bbox into Python for haversine.
    """
    _ = constraints
    lat = args.get("lat")
    lng = args.get("lng")
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return {"places": [], "note": "lat_lng_required"}
    if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        return {"places": [], "note": "coords_out_of_range"}
    radius_m = args.get("radius_m", 3000)
    if not isinstance(radius_m, (int, float)):
        radius_m = 3000
    radius_m = max(500, min(int(radius_m), 20_000))
    limit = args.get("limit", 6)
    if not isinstance(limit, int):
        limit = 6
    limit = max(1, min(limit, _MAX_PLACES))

    rows = (
        await session.scalars(
            places_near_point_stmt(lat=float(lat), lng=float(lng), radius_m=radius_m, limit=limit)
        )
    ).all()
    places = [
        {
            "place_id": str(place.id),
            "title": place.name[:80],
            "subtitle": (place.short_description or "")[:120] or None,
        }
        for place in rows
    ]
    return {"places": places, "near_lat": lat, "near_lng": lng, "radius_m": radius_m}


def places_near_point_stmt(
    *,
    lat: float,
    lng: float,
    radius_m: int,
    limit: int,
) -> Select[tuple[Place]]:
    """Published, open places within ``radius_m`` of a WGS84 point, nearest first."""
    origin = cast(func.ST_SetSRID(func.ST_MakePoint(lng, lat), 4326), Geography)
    return (
        select(Place)
        .where(
            Place.publication_status == "published",
            or_(
                Place.temporary_closure_status.is_(None),
                Place.temporary_closure_status == "none",
            ),
            func.ST_DWithin(Place.location, origin, radius_m),
        )
        .order_by(func.ST_Distance(Place.location, origin))
        .limit(limit)
    )


def _seasonal_recommendations(
    args: dict[str, Any],
    constraints: dict[str, Any],
) -> dict[str, Any]:
    raw_season = args.get("season") or constraints.get("season")
    if isinstance(raw_season, str) and raw_season.strip().casefold() in _SEASONAL_TIPS:
        season = raw_season.strip().casefold()
    else:
        season = season_from_month()
    items = list(_SEASONAL_TIPS.get(season, []))
    return {"season": season, "items": items[:4]}

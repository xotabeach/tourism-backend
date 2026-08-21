"""Allowlisted planning tools: LLM requests → backend → PostGIS → sanitized DATA."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.geography.infrastructure.models import Locality, Region
from tourism_backend.modules.places.infrastructure.models import Place

ToolHandler = Callable[..., Awaitable[dict[str, Any]]]

_MAX_PLACES = 8
_MAX_TOOL_ROUNDS = 2
_ALLOWED_TOOLS = frozenset({"search_places", "seasonal_recommendations"})


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
    if not city or city == "Крым":
        return {"places": [], "note": "city_required"}
    limit = args.get("limit", 6)
    if not isinstance(limit, int):
        limit = 6
    limit = max(1, min(limit, _MAX_PLACES))
    region = await session.scalar(select(Region).where(Region.slug == "crimea"))
    if region is None:
        return {"places": []}
    locality_ids = list(
        await session.scalars(
            select(Locality.id).where(
                Locality.region_id == region.id,
                Locality.status == "active",
                Locality.name.ilike(f"%{city}%"),
            )
        )
    )
    stmt = select(Place).where(
        Place.region_id == region.id,
        Place.publication_status == "published",
    )
    if locality_ids:
        stmt = stmt.where(
            or_(
                Place.locality_id.in_(locality_ids),
                Place.name.ilike(f"%{city}%"),
                Place.address.ilike(f"%{city}%"),
            )
        )
    else:
        stmt = stmt.where(
            or_(
                Place.name.ilike(f"%{city}%"),
                Place.address.ilike(f"%{city}%"),
            )
        )
    rows = (await session.scalars(stmt.limit(40))).all()
    interest = str(args.get("interest") or "").casefold()
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
        if city.casefold() in text or city.casefold() in (place.address or "").casefold():
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

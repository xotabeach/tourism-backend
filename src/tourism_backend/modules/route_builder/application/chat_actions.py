"""Allowlisted interactive action chips for AI route chat."""

from __future__ import annotations

from typing import Any, Literal

from tourism_backend.modules.route_builder.application.schemas import ActionsBlockOut

AskField = Literal[
    "city",
    "pace",
    "interests",
    "transport_mode",
    "duration",
    "people",
    "with_children",
    "ready",
]

_CONFIRMABLE_FIELDS: frozenset[str] = frozenset(
    {
        "city",
        "trip_type",
        "duration",
        "people",
        "interests",
        "pace",
        "season",
        "transport_mode",
        "day_kind",
        "budget_amount",
        "paid_ok",
        "with_children",
        "with_pets",
        "avoid_crowds",
    }
)

# Canonical id → label + optional constraint patch (+ field marked confirmed).
_ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "want_generate": {"label": "Подбери маршрут", "patch": None, "field": None},
    "pace_calm": {
        "label": "Хочу спокойно",
        "patch": {"pace": "calm"},
        "field": "pace",
    },
    "pace_moderate": {
        "label": "Умеренный темп",
        "patch": {"pace": "moderate"},
        "field": "pace",
    },
    "pace_active": {
        "label": "Хочу активно",
        "patch": {"pace": "active"},
        "field": "pace",
    },
    "interest_sea": {
        "label": "Больше моря",
        "patch": {"interests_add": ["море"]},
        "field": "interests",
    },
    "interest_mountains": {
        "label": "Больше гор",
        "patch": {"interests_add": ["горы"]},
        "field": "interests",
    },
    "interest_romance": {
        "label": "Романтика",
        "patch": {"interests_add": ["романтика"], "trip_type": "romance"},
        "field": "interests",
    },
    "with_children": {
        "label": "С детьми",
        "patch": {"with_children": True},
        "field": "with_children",
    },
    "transport_car": {
        "label": "На машине",
        "patch": {"transport_mode": "car"},
        "field": "transport_mode",
    },
    "transport_public": {
        "label": "Общественный транспорт",
        "patch": {"transport_mode": "public"},
        "field": "transport_mode",
    },
    "transport_walk": {
        "label": "Пешком",
        "patch": {"transport_mode": "walk"},
        "field": "transport_mode",
    },
    "transport_mixed": {
        "label": "Смешанный",
        "patch": {"transport_mode": "mixed"},
        "field": "transport_mode",
    },
    "duration_d1_2": {
        "label": "1–2 дня",
        "patch": {"duration": "d1_2"},
        "field": "duration",
    },
    "duration_d3_5": {
        "label": "3–5 дней",
        "patch": {"duration": "d3_5"},
        "field": "duration",
    },
    "duration_d6_7": {
        "label": "6–7 дней",
        "patch": {"duration": "d6_7"},
        "field": "duration",
    },
    "duration_d7plus": {
        "label": "Больше недели",
        "patch": {"duration": "d7plus"},
        "field": "duration",
    },
    "people_1": {"label": "Один", "patch": {"people": 1}, "field": "people"},
    "people_2": {"label": "Вдвоём", "patch": {"people": 2}, "field": "people"},
    "people_3_plus": {
        "label": "Компания (3+)",
        "patch": {"people": 3},
        "field": "people",
    },
}

# Legacy chip ids from the first interactive slice.
_ALIASES: dict[str, str] = {
    "say_mood_calm": "pace_calm",
    "say_mood_active": "pace_active",
    "say_more_sea": "interest_sea",
    "say_more_mountains": "interest_mountains",
    "say_with_kids": "with_children",
}

_ASK_FIELD_DEFAULTS: dict[str, tuple[str, ...]] = {
    "city": (),
    "pace": ("pace_calm", "pace_moderate", "pace_active"),
    "interests": (
        "interest_sea",
        "interest_mountains",
        "interest_romance",
        "with_children",
    ),
    "transport_mode": (
        "transport_car",
        "transport_public",
        "transport_walk",
        "transport_mixed",
    ),
    "duration": ("duration_d1_2", "duration_d3_5", "duration_d6_7", "duration_d7plus"),
    "people": ("people_1", "people_2", "people_3_plus"),
    "with_children": ("with_children",),
    "ready": ("want_generate",),
}

_MISSING_PRIORITY: tuple[str, ...] = (
    "city",
    "pace",
    "interests",
    "duration",
    "transport_mode",
    "people",
)

_MAX_ACTIONS = 6


def normalize_action_id(action_id: str) -> str | None:
    raw = action_id.strip()
    if not raw:
        return None
    canonical = _ALIASES.get(raw, raw)
    if canonical in _ACTION_CATALOG:
        return canonical
    return None


def action_label(action_id: str) -> str | None:
    canonical = normalize_action_id(action_id)
    if canonical is None:
        return None
    return str(_ACTION_CATALOG[canonical]["label"])


def patch_for_action(action_id: str) -> dict[str, Any] | None:
    canonical = normalize_action_id(action_id)
    if canonical is None:
        return None
    patch = _ACTION_CATALOG[canonical].get("patch")
    if not isinstance(patch, dict):
        return None
    return dict(patch)


def field_for_action(action_id: str) -> str | None:
    canonical = normalize_action_id(action_id)
    if canonical is None:
        return None
    field = _ACTION_CATALOG[canonical].get("field")
    return str(field) if isinstance(field, str) else None


def sanitize_confirmed_fields(raw: list[str] | None) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        key = item.strip()
        if key not in _CONFIRMABLE_FIELDS or key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= 24:
            break
    return out


def known_constraints(
    constraints: dict[str, Any],
    confirmed_fields: list[str],
) -> dict[str, Any]:
    confirmed = set(sanitize_confirmed_fields(confirmed_fields))
    return {key: constraints[key] for key in confirmed if key in constraints}


def unknown_fields(confirmed_fields: list[str]) -> list[str]:
    confirmed = set(sanitize_confirmed_fields(confirmed_fields))
    return [field for field in _MISSING_PRIORITY if field not in confirmed]


def first_missing_ask_field(confirmed_fields: list[str]) -> str:
    missing = unknown_fields(confirmed_fields)
    if not missing:
        return "ready"
    return missing[0]


def merge_constraint_patch(
    constraints: dict[str, Any],
    patch: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge allowlisted patch keys into constraints (interests_add special)."""
    if not patch:
        return dict(constraints)
    merged = dict(constraints)
    interests_add = patch.get("interests_add")
    for key, value in patch.items():
        if key == "interests_add":
            continue
        if key not in _CONFIRMABLE_FIELDS:
            continue
        merged[key] = value
    if isinstance(interests_add, list):
        current = list(merged.get("interests") or [])
        seen = {str(item).casefold() for item in current}
        for raw in interests_add:
            item = str(raw).strip()
            if not item or item.casefold() in seen:
                continue
            seen.add(item.casefold())
            current.append(item)
            if len(current) >= 12:
                break
        merged["interests"] = current
    return merged


def fields_touched_by_patch(patch: dict[str, Any] | None) -> list[str]:
    if not patch:
        return []
    out: list[str] = []
    for key in patch:
        if key == "interests_add":
            out.append("interests")
        elif key in _CONFIRMABLE_FIELDS:
            out.append(key)
    return sanitize_confirmed_fields(out)


def build_actions_block(
    *,
    action_ids: list[str] | None = None,
    ask_field: str | None = None,
    confirmed_fields: list[str] | None = None,
    include_generate: bool = True,
) -> list[ActionsBlockOut]:
    """Build chips from explicit ids, ask_field defaults, or first missing field."""
    ids: list[str] = []
    if action_ids:
        for raw in action_ids[:_MAX_ACTIONS]:
            canonical = normalize_action_id(raw)
            if canonical and canonical not in ids:
                ids.append(canonical)
    if not ids:
        field = ask_field or first_missing_ask_field(confirmed_fields or [])
        if field not in _ASK_FIELD_DEFAULTS:
            field = first_missing_ask_field(confirmed_fields or [])
        ids = list(_ASK_FIELD_DEFAULTS.get(field, ()))
    if (
        include_generate
        and "want_generate" not in ids
        and (ask_field == "ready" or not unknown_fields(confirmed_fields or []))
    ):
        ids = ["want_generate", *ids]
    # Offer generate as escape hatch once a couple of fields are confirmed.
    if (
        include_generate
        and "want_generate" not in ids
        and len(ids) < _MAX_ACTIONS
        and (ask_field in {None, "ready"} or len(sanitize_confirmed_fields(confirmed_fields)) >= 2)
    ):
        ids.append("want_generate")

    actions: list[dict[str, str]] = []
    for action_id in ids[:_MAX_ACTIONS]:
        label = action_label(action_id)
        if label is None:
            continue
        actions.append({"id": action_id, "label": label})
    if not actions:
        actions = [{"id": "want_generate", "label": "Подбери маршрут"}]
    return [ActionsBlockOut(actions=actions)]


def clarification_action_blocks(
    constraints: dict[str, Any] | None = None,
    *,
    confirmed_fields: list[str] | None = None,
    ask_field: str | None = None,
    action_ids: list[str] | None = None,
) -> list[ActionsBlockOut]:
    """Dynamic chips for clarification / greeting assistant text."""
    _ = constraints
    return build_actions_block(
        action_ids=action_ids,
        ask_field=ask_field,
        confirmed_fields=confirmed_fields,
    )

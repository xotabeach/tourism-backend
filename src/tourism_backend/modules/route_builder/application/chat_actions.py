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
    "build_custom_route": {
        "label": "Собрать собственный маршрут",
        "patch": None,
        "field": None,
    },
    "clear_params": {"label": "Очистить мои параметры", "patch": None, "field": None},
    "pace_calm": {
        "label": "Спокойный маршрут",
        "patch": {"pace": "calm"},
        "field": "pace",
    },
    "pace_moderate": {
        "label": "Умеренный темп",
        "patch": {"pace": "moderate"},
        "field": "pace",
    },
    "pace_active": {
        "label": "Активный маршрут",
        "patch": {"pace": "active"},
        "field": "pace",
    },
    "interest_sea": {
        "label": "Путешествие к морю",
        "patch": {"interests_add": ["море"]},
        "field": "interests",
    },
    "interest_mountains": {
        "label": "Маршрут по горам",
        "patch": {"interests_add": ["горы"]},
        "field": "interests",
    },
    "interest_food": {
        "label": "Гастрономический тур",
        "patch": {"interests_add": ["еда"]},
        "field": "interests",
    },
    "interest_romance": {
        "label": "Романтика",
        "patch": {"interests_add": ["романтика"], "trip_type": "romance"},
        "field": "interests",
    },
    "interest_history": {
        "label": "История",
        "patch": {"interests_add": ["история"]},
        "field": "interests",
    },
    "interest_nature": {
        "label": "Природа",
        "patch": {"interests_add": ["природа"]},
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
    # Budget is a slider control: this action only opens the budget slider
    # (sets ask_field=budget); the actual value comes from the control, not a
    # constraint patch.
    "ask_budget": {
        "label": "Бюджет",
        "patch": None,
        "field": "budget_amount",
    },
    # City picker options — kept in sync with mobile's `_allCities` list
    # (route_match_screen.dart) so the chat sheet and the params-form
    # autocomplete offer the same set of localities.
    "city_simferopol": {
        "label": "Симферополь",
        "patch": {"city": "Симферополь"},
        "field": "city",
    },
    "city_yalta": {"label": "Ялта", "patch": {"city": "Ялта"}, "field": "city"},
    "city_alushta": {
        "label": "Алушта",
        "patch": {"city": "Алушта"},
        "field": "city",
    },
    "city_saki": {"label": "Саки", "patch": {"city": "Саки"}, "field": "city"},
    "city_sevastopol": {
        "label": "Севастополь",
        "patch": {"city": "Севастополь"},
        "field": "city",
    },
    "city_evpatoria": {
        "label": "Евпатория",
        "patch": {"city": "Евпатория"},
        "field": "city",
    },
    "city_feodosia": {
        "label": "Феодосия",
        "patch": {"city": "Феодосия"},
        "field": "city",
    },
    "city_kerch": {"label": "Керчь", "patch": {"city": "Керчь"}, "field": "city"},
    "city_bakhchisaray": {
        "label": "Бахчисарай",
        "patch": {"city": "Бахчисарай"},
        "field": "city",
    },
    "city_sudak": {"label": "Судак", "patch": {"city": "Судак"}, "field": "city"},
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
    "city": (
        "city_simferopol",
        "city_yalta",
        "city_alushta",
        "city_saki",
        "city_sevastopol",
        "city_evpatoria",
        "city_feodosia",
        "city_kerch",
        "city_bakhchisaray",
        "city_sudak",
    ),
    "pace": ("pace_calm", "pace_moderate", "pace_active"),
    "interests": (
        "interest_sea",
        "interest_mountains",
        "interest_history",
        "interest_nature",
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
    "budget": ("ask_budget",),
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
# City chips render in a bottom-sheet list (not inline chips), so they can
# hold the full locality set without cluttering the message bubble.
_MAX_SHEET_ACTIONS = 12
_SHEET_TITLES: dict[str, str] = {"city": "Выбрать город"}


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
    *,
    previously_confirmed: list[str] | None = None,
) -> dict[str, Any]:
    """Merge allowlisted patch into working constraints.

    Form values may already sit in ``constraints`` as a *draft*. Chat statements
    win for newly confirmed list fields: the first ``interests_add`` while
    ``interests`` is not yet confirmed replaces the draft list instead of
    appending to it (so form «природа» does not become a chat fact).
    """
    if not patch:
        return dict(constraints)
    confirmed_before = set(sanitize_confirmed_fields(previously_confirmed))
    merged = dict(constraints)
    interests_add = patch.get("interests_add")
    for key, value in patch.items():
        if key == "interests_add":
            continue
        if key not in _CONFIRMABLE_FIELDS:
            continue
        merged[key] = value
    if isinstance(interests_add, list):
        current = list(merged.get("interests") or []) if "interests" in confirmed_before else []
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


def form_draft_constraints(
    constraints: dict[str, Any],
    confirmed_fields: list[str],
) -> dict[str, Any]:
    """Working params that are still draft (not stated in this chat)."""
    confirmed = set(sanitize_confirmed_fields(confirmed_fields))
    draft: dict[str, Any] = {}
    for key, value in constraints.items():
        if key not in _CONFIRMABLE_FIELDS or key in confirmed:
            continue
        if value is None or value == "" or value == []:
            continue
        # Generic region placeholder is not a useful draft city.
        if key == "city" and str(value).strip() in {"", "Крым"}:
            continue
        draft[key] = value
    return draft


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
    resolved_field: str | None = None
    if action_ids:
        for raw in action_ids[:_MAX_ACTIONS]:
            canonical = normalize_action_id(raw)
            if canonical and canonical not in ids:
                ids.append(canonical)
    if not ids:
        field = ask_field or first_missing_ask_field(confirmed_fields or [])
        if field not in _ASK_FIELD_DEFAULTS:
            field = first_missing_ask_field(confirmed_fields or [])
        resolved_field = field
        ids = list(_ASK_FIELD_DEFAULTS.get(field, ()))
    cap = _MAX_SHEET_ACTIONS if resolved_field in _SHEET_TITLES else _MAX_ACTIONS
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
        and len(ids) < cap
        and (ask_field in {None, "ready"} or len(sanitize_confirmed_fields(confirmed_fields)) >= 2)
    ):
        ids.append("want_generate")

    actions: list[dict[str, str]] = []
    for action_id in ids[:cap]:
        label = action_label(action_id)
        if label is None:
            continue
        actions.append({"id": action_id, "label": label})
    if not actions:
        actions = [{"id": "want_generate", "label": "Подбери маршрут"}]
    layout: Literal["wrap", "stack", "sheet"] = (
        "sheet" if resolved_field in _SHEET_TITLES else "wrap"
    )
    sheet_title = _SHEET_TITLES.get(resolved_field) if resolved_field else None
    return [ActionsBlockOut(actions=actions, layout=layout, sheet_title=sheet_title)]


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


def interactive_control_blocks(
    *,
    ask_field: str | None,
    constraints: dict[str, Any] | None = None,
) -> list[Any]:
    """Slider / toggle blocks matched to the current ask_field."""
    from tourism_backend.modules.route_builder.application.schemas import (
        SliderBlockOut,
        ToggleBlockOut,
    )

    constraints = constraints or {}
    out: list[Any] = []
    if ask_field in {"budget", "ready"}:
        current = constraints.get("budget_amount")
        value = float(current) if isinstance(current, int) else 3000.0
        out.append(
            SliderBlockOut(
                id="budget_amount",
                label="Бюджет на день, ₽",
                min_value=0,
                max_value=20000,
                step=500,
                value=value,
                unit="₽",
            )
        )
    if ask_field in {"with_children", "ready", "people"}:
        out.append(
            ToggleBlockOut(
                id="with_children",
                label="Едем с детьми",
                value=bool(constraints.get("with_children")),
            )
        )
        out.append(
            ToggleBlockOut(
                id="with_pets",
                label="С питомцами",
                value=bool(constraints.get("with_pets")),
            )
        )
    return out


def prefer_ready_ask_field(confirmed_fields: list[str]) -> str:
    """Fewer quiz turns: city + one preference is enough to offer generate."""
    confirmed = set(sanitize_confirmed_fields(confirmed_fields))
    if "city" in confirmed and confirmed & {"pace", "interests", "season", "duration"}:
        return "ready"
    if len(confirmed) >= 3:
        return "ready"
    return first_missing_ask_field(confirmed_fields)

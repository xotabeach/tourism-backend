"""Provider-neutral bounded DATA: never slice a serialized JSON document."""

import json
from typing import Any

from tourism_backend.modules.route_builder.application.chat_actions import (
    known_constraints,
    prefer_ready_ask_field,
    unknown_fields,
)


def _compact(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return None
    if isinstance(value, str):
        return value[:1200]
    if isinstance(value, list):
        return [_compact(item, depth + 1) for item in value[:6]]
    if isinstance(value, dict):
        return {str(key)[:80]: _compact(item, depth + 1) for key, item in list(value.items())[:16]}
    return value if value is None or isinstance(value, (bool, int, float)) else str(value)[:120]


def planning_state_note(
    constraints: dict[str, Any],
    confirmed: list[str],
    place_hints: list[dict[str, str]] | None,
    tool_context: dict[str, Any] | None,
) -> str:
    data: dict[str, Any] = {}
    context = dict(tool_context or {})
    if place_hints and "place_candidates" not in context:
        context["place_candidates"] = place_hints[:4]
    priority = (
        "search_context",
        "tool_results",
        "catalog_routes",
        "comparison_routes",
        "knowledge",
        "user_preferences_prior",
        "place_candidates",
        "seasonal_recommendations",
        "form_draft_not_facts",
    )
    for key in dict.fromkeys([*priority, *context]):
        if key not in context or key.startswith("_"):
            continue
        value = _compact(context[key])
        budget = 6000 if key == "knowledge" else 3000
        while isinstance(value, list) and len(json.dumps(value, ensure_ascii=False)) > budget:
            value.pop()
        if len(json.dumps(value, ensure_ascii=False)) <= budget:
            candidate = {**data, key: value}
            if len(json.dumps(candidate, ensure_ascii=False)) <= 18000:
                data = candidate
    return (
        "Этап (backend): "
        + str(constraints.get("dialogue_goal") or "clarify")
        + "\nИзвестно (JSON, только подтверждённые пользователем поля): "
        + json.dumps(known_constraints(constraints, confirmed), ensure_ascii=False)
        + "\nНеизвестно (не выдумывай): "
        + json.dumps(unknown_fields(confirmed), ensure_ascii=False)
        + "\nПодсказка ask_field (не обязательная анкета): "
        + prefer_ready_ask_field(confirmed)
        + "\nbackend_DATA: "
        + json.dumps(data, ensure_ascii=False)
    )

"""Parse / repair structured JSON turns from the planning LLM."""

from __future__ import annotations

import json
import re
from typing import Any

from tourism_backend.modules.route_builder.application.ai import StructuredChatTurn
from tourism_backend.modules.route_builder.application.chat_actions import (
    first_missing_ask_field,
    normalize_action_id,
    sanitize_confirmed_fields,
)

_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")
_MAX_ACTION_IDS = 6
_MAX_TEXT = 600
_ASK_FIELDS = frozenset(
    {
        "city",
        "pace",
        "interests",
        "transport_mode",
        "duration",
        "people",
        "with_children",
        "ready",
    }
)
_PATCH_KEYS = frozenset(
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
        "interests_add",
    }
)


def parse_structured_turn(
    raw: str,
    *,
    confirmed_fields: list[str] | None = None,
) -> StructuredChatTurn | None:
    payload = _extract_json_object(raw)
    if payload is None:
        return None
    text = payload.get("assistant_text")
    if not isinstance(text, str) or not text.strip():
        return None
    ask_raw = payload.get("ask_field")
    ask_field: str | None = None
    if isinstance(ask_raw, str) and ask_raw.strip() in _ASK_FIELDS:
        ask_field = ask_raw.strip()
    if ask_field is None:
        ask_field = first_missing_ask_field(confirmed_fields or [])

    action_ids: list[str] = []
    raw_ids = payload.get("action_ids")
    if isinstance(raw_ids, list):
        for item in raw_ids[:_MAX_ACTION_IDS]:
            if not isinstance(item, str):
                continue
            canonical = normalize_action_id(item)
            if canonical and canonical not in action_ids:
                action_ids.append(canonical)

    patch = _sanitize_patch(payload.get("constraint_patch"))
    return StructuredChatTurn(
        assistant_text=text.strip()[:_MAX_TEXT],
        ask_field=ask_field,
        action_ids=tuple(action_ids),
        constraint_patch=patch,
    )


def fallback_structured_turn(
    *,
    confirmed_fields: list[str] | None = None,
    user_snippet: str = "",
) -> StructuredChatTurn:
    ask = first_missing_ask_field(confirmed_fields or [])
    known = sanitize_confirmed_fields(confirmed_fields)
    snippet = user_snippet.strip()
    if len(snippet) > 60:
        snippet = f"{snippet[:57]}..."
    if ask == "ready":
        text = (
            "Параметров достаточно. Нажмите «Подбери маршрут» или напишите «давай» — "
            "соберу карточку предложения."
        )
    elif snippet:
        text = f"Принял: «{snippet}». Уточните, пожалуйста: {_ask_prompt(ask)}"
    elif known:
        text = f"Уточните, пожалуйста: {_ask_prompt(ask)}"
    else:
        text = (
            "Помогу с маршрутом по Крыму. С какого города стартуем или какой темп поездки хотите?"
        )
        ask = "city" if ask == "ready" else ask
    return StructuredChatTurn(
        assistant_text=text,
        ask_field=ask,
        action_ids=(),
        constraint_patch={},
    )


def _ask_prompt(ask_field: str) -> str:
    prompts = {
        "city": "город старта?",
        "pace": "спокойный, умеренный или активный темп?",
        "interests": "море, горы или другой фокус?",
        "transport_mode": "машина, общественный транспорт или пешком?",
        "duration": "на сколько дней поездка?",
        "people": "сколько человек едет?",
        "with_children": "едете с детьми?",
        "ready": "готовы подобрать маршрут?",
    }
    return prompts.get(ask_field, "что важно для поездки?")


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    match = _JSON_OBJECT_RE.search(raw)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _sanitize_patch(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in _PATCH_KEYS:
            continue
        if key == "interests_add" and isinstance(value, list):
            items = [str(item).strip()[:40] for item in value if str(item).strip()]
            if items:
                out["interests_add"] = items[:6]
            continue
        if key == "interests" and isinstance(value, list):
            items = [str(item).strip()[:40] for item in value if str(item).strip()]
            if items:
                out["interests"] = items[:12]
            continue
        if key in {"people", "budget_amount"} and isinstance(value, int):
            out[key] = value
            continue
        if key in {"paid_ok", "with_children", "with_pets", "avoid_crowds"} and isinstance(
            value, bool
        ):
            out[key] = value
            continue
        if isinstance(value, str):
            text = value.strip()[:80]
            if text:
                out[key] = text
    return out

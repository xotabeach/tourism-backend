"""AI draft of a timetable form from text an editor pasted (spec 12b, D8).

The model only fills the form; nothing is saved until the editor checks
and submits it, and the form validation runs as for a hand-filled one.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from tourism_backend.modules.route_builder.application.structured_turn import (
    extract_json_object,
)
from tourism_backend.modules.transit.application.schedules import WEEKDAYS, parse_times

MAX_SOURCE_CHARS = 6_000

_SYSTEM_PROMPT = (
    "Ты помогаешь редактору перенести расписание общественного транспорта Крыма в форму. "
    "Из текста расписания верни только JSON без markdown, массив periods. Каждый период: "
    '{"title": "Будни" или "Лето, ежедневно", "days": [0..6, где 0 понедельник], '
    '"date_from": "ГГГГ-ММ-ДД" или null, "date_to": "ГГГГ-ММ-ДД" или null, '
    '"headway_minutes": число или null, "first_departure": "ЧЧ:ММ" или null, '
    '"last_departure": "ЧЧ:ММ" или null, "departures": ["ЧЧ:ММ", ...] или [], '
    '"note": короткое пояснение или null}. '
    "Интервал ставь, только если в тексте сказано «каждые N минут» или «интервал»; "
    "если перечислены конкретные отправления, заполни departures и оставь интервал null. "
    "Бери отправления от начальной остановки направления, указанного редактором, "
    "или от первой в тексте. Ничего не выдумывай: чего нет в тексте, оставь null. "
    'Формат ответа: {"periods": [...], "warning": null или что было неясно}.'
)


class TextCompleter(Protocol):
    async def complete_text(self, *, system: str, user: str, max_tokens: int) -> str: ...


def _hhmm(raw: object) -> str:
    values = parse_times(str(raw)) if raw else []
    return values[0].strftime("%H:%M") if values else ""


def draft_to_forms(payload: dict[str, Any]) -> tuple[list[dict[str, str]], str | None]:
    """Model output into admin form values; drops what does not parse."""
    forms: list[dict[str, str]] = []
    for period in payload.get("periods") or []:
        if not isinstance(period, dict):
            continue
        form: dict[str, str] = {
            "title": str(period.get("title") or "").strip()[:128],
            "date_from": str(period.get("date_from") or "")[:10],
            "date_to": str(period.get("date_to") or "")[:10],
            "first_departure": _hhmm(period.get("first_departure")),
            "last_departure": _hhmm(period.get("last_departure")),
            "note": str(period.get("note") or "").strip(),
        }
        headway = period.get("headway_minutes")
        form["headway_minutes"] = str(headway) if isinstance(headway, int) and headway > 0 else ""
        departures = period.get("departures") or []
        if isinstance(departures, list):
            form["departures"] = " ".join(
                value.strftime("%H:%M") for value in parse_times(" ".join(map(str, departures)))
            )
        for index in period.get("days") or []:
            if isinstance(index, int) and 0 <= index < len(WEEKDAYS):
                form[f"day_{index}"] = "on"
        forms.append(form)
    warning = payload.get("warning")
    return forms, str(warning) if warning else None


async def draft_schedule(
    completer: TextCompleter, *, line_name: str, direction: str | None, text: str
) -> tuple[list[dict[str, str]], str | None]:
    user = json.dumps(
        {"line": line_name, "direction": direction, "text": text[:MAX_SOURCE_CHARS]},
        ensure_ascii=False,
    )
    raw = await completer.complete_text(system=_SYSTEM_PROMPT, user=user, max_tokens=1_500)
    payload = extract_json_object(raw)
    if payload is None:
        raise ValueError("AI returned no JSON")
    return draft_to_forms(payload)

"""Deterministic mock AI planning provider (no network)."""

from __future__ import annotations

import json
from typing import Any

from tourism_backend.modules.route_builder.application.ai import (
    AIProviderProbeResult,
    ChatMessage,
    ChatTurnResult,
)
from tourism_backend.modules.route_builder.application.chat_actions import (
    first_missing_ask_field,
    known_constraints,
)
from tourism_backend.modules.route_builder.application.structured_turn import (
    fallback_structured_turn,
)


class MockAIPlanningProvider:
    async def probe(self) -> AIProviderProbeResult:
        return AIProviderProbeResult(
            provider="mock",
            configured_model="mock-route-assistant",
            available_models=("mock-route-assistant",),
            response_text='{"status":"ok","language":"ru"}',
        )

    async def chat_turn(
        self,
        *,
        messages: list[ChatMessage],
        constraints: dict[str, Any],
        confirmed_fields: list[str] | None = None,
        place_hints: list[dict[str, str]] | None = None,
        max_tokens: int = 320,
    ) -> ChatTurnResult:
        _ = max_tokens
        _ = place_hints
        confirmed = list(confirmed_fields or [])
        known = known_constraints(constraints, confirmed)
        last_user = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        ask = first_missing_ask_field(confirmed)
        structured = fallback_structured_turn(
            confirmed_fields=confirmed,
            user_snippet=last_user,
        )
        # Prefer ask_field from missing; keep fallback text but attach field-specific ids.
        action_ids: tuple[str, ...] = ()
        if ask == "transport_mode":
            action_ids = ("transport_car", "transport_public", "transport_walk")
            text = (
                "Принято. Подскажите, вы планируете поездку на машине, "
                "общественным транспортом или пешком?"
            )
        elif ask == "pace":
            action_ids = ("pace_calm", "pace_moderate", "pace_active")
            text = "Какой темп вам ближе — спокойный, умеренный или активный?"
        elif ask == "interests":
            action_ids = ("interest_sea", "interest_mountains", "interest_romance")
            text = "Что важнее — море, горы или романтика?"
        elif ask == "duration":
            action_ids = ("duration_d1_2", "duration_d3_5", "duration_d6_7")
            text = "На сколько дней планируете поездку?"
        elif ask == "people":
            action_ids = ("people_1", "people_2", "people_3_plus")
            text = "Сколько человек едет?"
        elif ask == "city":
            action_ids = ()
            text = (
                "С какого города в Крыму стартуем? Напишите название города "
                "(например Севастополь или Алушта)."
            )
        elif ask == "ready":
            action_ids = ("want_generate",)
            city = known.get("city")
            city_bit = f" вокруг {city}" if city else ""
            text = (
                f"Параметров достаточно{city_bit}. Нажмите «Подбери маршрут» или напишите «давай»."
            )
        else:
            text = structured.assistant_text
            ask = structured.ask_field or ask

        # Deterministic tiny patch only when user text clearly matches known chip labels.
        patch: dict[str, Any] = {}
        lowered = last_user.casefold()
        if "спокойн" in lowered:
            patch = {"pace": "calm"}
            ask = first_missing_ask_field([*confirmed, "pace"])
        elif "активн" in lowered:
            patch = {"pace": "active"}
            ask = first_missing_ask_field([*confirmed, "pace"])
        elif "мор" in lowered:
            patch = {"interests_add": ["море"]}
            ask = first_missing_ask_field([*confirmed, "interests"])
        elif "гор" in lowered:
            patch = {"interests_add": ["горы"]}
            ask = first_missing_ask_field([*confirmed, "interests"])

        _ = json.dumps(known, ensure_ascii=False)  # keep known path exercised
        return ChatTurnResult(
            assistant_text=text,
            proposed_constraints=patch or None,
            ask_field=ask,
            action_ids=action_ids,
            provider="mock",
        )

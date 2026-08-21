"""Deterministic mock AI planning provider (no network)."""

from __future__ import annotations

from typing import Any

from tourism_backend.modules.route_builder.application.ai import (
    AIProviderProbeResult,
    ChatMessage,
    ChatTurnResult,
)
from tourism_backend.modules.route_builder.application.chat_actions import (
    first_missing_ask_field,
    known_constraints,
    prefer_ready_ask_field,
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
        tool_context: dict[str, Any] | None = None,
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
        ask = prefer_ready_ask_field(confirmed)
        if ask != "ready":
            ask = first_missing_ask_field(confirmed)

        seasonal = (tool_context or {}).get("seasonal_recommendations") or []
        tip_title = ""
        if isinstance(seasonal, list) and seasonal:
            first = seasonal[0]
            if isinstance(first, dict):
                tip_title = str(first.get("title") or "")

        action_ids: tuple[str, ...] = ()
        if ask == "transport_mode":
            action_ids = ("transport_car", "transport_public", "transport_walk")
            text = (
                "Принято. Подскажите транспорт — или нажмите «Подбери маршрут», "
                "если хотите сразу карточку."
            )
        elif ask == "pace":
            action_ids = ("pace_calm", "pace_moderate", "pace_active", "want_generate")
            text = (
                f"{tip_title + '. ' if tip_title else ''}"
                "Какой темп ближе — или сразу подберём маршрут?"
            )
        elif ask == "interests":
            action_ids = ("interest_sea", "interest_mountains", "want_generate")
            text = "Что важнее — море, горы — или сразу «Подбери маршрут»?"
        elif ask == "duration":
            action_ids = ("duration_d1_2", "duration_d3_5", "want_generate")
            text = "На сколько дней — или собрать предложение сейчас?"
        elif ask == "people":
            action_ids = ("people_1", "people_2", "people_3_plus", "want_generate")
            text = "Сколько человек едет?"
        elif ask == "city":
            action_ids = ("want_generate",)
            text = (
                f"{tip_title + '. ' if tip_title else ''}"
                "С какого города стартуем? Можно принять рекомендацию ниже "
                "или написать город."
            )
            # Ask backend for seasonal tips via tool loop on first turn.
            return ChatTurnResult(
                assistant_text=text,
                ask_field=ask,
                action_ids=action_ids,
                tool_requests=({"name": "seasonal_recommendations", "arguments": {}},),
                provider="mock",
            )
        elif ask == "ready":
            action_ids = ("want_generate",)
            city = known.get("city")
            city_bit = f" вокруг {city}" if city else ""
            text = (
                f"Параметров достаточно{city_bit}. Нажмите «Подбери маршрут» или напишите «давай»."
            )
        else:
            structured = fallback_structured_turn(
                confirmed_fields=confirmed,
                user_snippet=last_user,
            )
            text = structured.assistant_text
            ask = structured.ask_field or ask

        patch: dict[str, Any] = {}
        lowered = last_user.casefold()
        if "спокойн" in lowered:
            patch = {"pace": "calm"}
            ask = prefer_ready_ask_field([*confirmed, "pace"])
        elif "активн" in lowered:
            patch = {"pace": "active"}
            ask = prefer_ready_ask_field([*confirmed, "pace"])
        elif "мор" in lowered:
            patch = {"interests_add": ["море"]}
            ask = prefer_ready_ask_field([*confirmed, "interests"])
        elif "гор" in lowered:
            patch = {"interests_add": ["горы"]}
            ask = prefer_ready_ask_field([*confirmed, "interests"])

        return ChatTurnResult(
            assistant_text=text,
            proposed_constraints=patch or None,
            ask_field=ask,
            action_ids=action_ids,
            provider="mock",
        )

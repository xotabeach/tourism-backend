"""Deterministic mock AI planning provider (no network)."""

from __future__ import annotations

from typing import Any

from tourism_backend.modules.route_builder.application.ai import (
    AIProviderProbeResult,
    ChatMessage,
    ChatTurnResult,
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
        max_tokens: int = 256,
    ) -> ChatTurnResult:
        _ = max_tokens
        city = str(constraints.get("city") or "Крыму")
        last_user = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        snippet = last_user.strip()
        if len(snippet) > 80:
            snippet = f"{snippet[:77]}..."
        text = (
            f"Понял: «{snippet}». Опираюсь на параметры вокруг {city}. "
            "Могу уточнить настроение или интересы — либо нажмите "
            "«Подбери маршрут», и соберу карточку предложения."
        )
        return ChatTurnResult(assistant_text=text, provider="mock")

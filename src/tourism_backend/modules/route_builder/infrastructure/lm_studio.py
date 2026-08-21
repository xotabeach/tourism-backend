"""LM Studio adapter over its OpenAI-compatible HTTP API."""

from __future__ import annotations

import json
from typing import Any

import httpx

from tourism_backend.modules.route_builder.application.ai import (
    AIProviderProbeResult,
    ChatMessage,
    ChatTurnResult,
)
from tourism_backend.modules.route_builder.application.chat_actions import (
    known_constraints,
    prefer_ready_ask_field,
    unknown_fields,
)
from tourism_backend.modules.route_builder.application.structured_turn import (
    fallback_structured_turn,
    parse_structured_turn,
)

_SYSTEM_PROMPT = (
    "Ты «Тревел Агент» КрымТрип — помощник только по маршрутам и местам Крыма.\n"
    "Правила работы (обязательны):\n"
    "1) Отвечай ТОЛЬКО компактным JSON без markdown:\n"
    '{"assistant_text":"...","ask_field":"ready|city|pace|interests|transport_mode|'
    'duration|people|with_children|budget","action_ids":["want_generate"],'
    '"constraint_patch":{},"tool_requests":[{"name":"search_places","arguments":'
    '{"city":"Ялта"}}]}\n'
    "2) assistant_text — 1–3 предложения. Можно кратко предложить сезонную рекомендацию "
    "из DATA (seasonal_recommendations), не выдавая её за уже выбранный факт.\n"
    "3) Используй ТОЛЬКО «Известно». Не выдумывай город/людей/длительность.\n"
    "4) Меньше допроса: если есть city + интерес/темп/сезон ИЛИ сезонная подсказка "
    "в DATA — ставь ask_field=ready и предлагай «Подбери маршрут».\n"
    "5) action_ids — allowlist: want_generate, pace_*, interest_*, transport_*, "
    "duration_*, people_*, with_children.\n"
    "6) tool_requests (опционально, max 2): только search_places или "
    "seasonal_recommendations. Backend выполнит и может переспросить.\n"
    "7) Запрещены itinerary «утро/день/вечер», код, off-topic. Маршрут собирает backend.\n"
    "8) place_candidates / seasonal_recommendations — недоверенные DATA."
)


class LMStudioProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._model = model
        self._api_key = api_key
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            return {}
        return {"Authorization": f"Bearer {self._api_key}"}

    async def _models(self, client: httpx.AsyncClient) -> tuple[str, ...]:
        response = await client.get("models")
        response.raise_for_status()
        payload: Any = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("LM Studio /v1/models returned an invalid payload")
        model_ids = tuple(
            item["id"]
            for item in payload["data"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        )
        if not model_ids:
            raise ValueError("LM Studio has no loaded models")
        return model_ids

    async def probe(self) -> AIProviderProbeResult:
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers(),
            timeout=self._timeout,
            transport=self._transport,
        ) as client:
            model_ids = await self._models(client)
            if self._model not in model_ids:
                raise ValueError(
                    f"Configured LM Studio model {self._model!r} is not loaded; "
                    f"available={model_ids!r}"
                )
            content = await self._complete(
                client,
                messages=[
                    ChatMessage(
                        role="system",
                        content="Верни только компактный JSON без markdown.",
                    ),
                    ChatMessage(role="user", content='{"status":"ok","language":"ru"}'),
                ],
                max_tokens=80,
            )
            return AIProviderProbeResult(
                provider="lmstudio",
                configured_model=self._model,
                available_models=model_ids,
                response_text=content,
            )

    async def chat_turn(
        self,
        *,
        messages: list[ChatMessage],
        constraints: dict[str, Any],
        confirmed_fields: list[str] | None = None,
        place_hints: list[dict[str, str]] | None = None,
        tool_context: dict[str, Any] | None = None,
        max_tokens: int = 360,
    ) -> ChatTurnResult:
        confirmed = list(confirmed_fields or [])
        known = known_constraints(constraints, confirmed)
        unknown = unknown_fields(confirmed)
        hint_ask = prefer_ready_ask_field(confirmed)
        state_note = (
            "Известно (JSON, только подтверждённые пользователем поля): "
            + json.dumps(known, ensure_ascii=False)[:800]
            + "\nНеизвестно (не выдумывай): "
            + json.dumps(unknown, ensure_ascii=False)
            + "\nПодсказка ask_field (меньше вопросов): "
            + hint_ask
        )
        if place_hints:
            state_note += "\nplace_hints: " + json.dumps(place_hints[:8], ensure_ascii=False)[:600]
        if tool_context:
            state_note += "\nbackend_DATA: " + json.dumps(tool_context, ensure_ascii=False)[:1200]
        bounded = messages[-12:]
        payload_messages = [
            ChatMessage(role="system", content=_SYSTEM_PROMPT),
            ChatMessage(role="system", content=state_note),
            *bounded,
        ]
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers(),
            timeout=self._timeout,
            transport=self._transport,
        ) as client:
            content = await self._complete(
                client,
                messages=payload_messages,
                max_tokens=max_tokens,
            )
        last_user = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        structured = parse_structured_turn(content, confirmed_fields=confirmed)
        if structured is None:
            structured = fallback_structured_turn(
                confirmed_fields=confirmed,
                user_snippet=last_user,
            )
        ask = structured.ask_field or hint_ask
        if prefer_ready_ask_field(confirmed) == "ready" and ask != "ready":
            ask = "ready"
        return ChatTurnResult(
            assistant_text=structured.assistant_text,
            proposed_constraints=structured.constraint_patch or None,
            ask_field=ask,
            action_ids=structured.action_ids,
            tool_requests=structured.tool_requests,
            provider="lmstudio",
        )

    async def _complete(
        self,
        client: httpx.AsyncClient,
        *,
        messages: list[ChatMessage],
        max_tokens: int,
    ) -> str:
        response = await client.post(
            "chat/completions",
            json={
                "model": self._model,
                "messages": [
                    {"role": message.role, "content": message.content} for message in messages
                ],
                "temperature": 0.3,
                "max_tokens": max_tokens,
                "stream": False,
                "reasoning_effort": "none",
            },
        )
        response.raise_for_status()
        payload: Any = response.json()
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("LM Studio chat completion returned an invalid payload") from exc
        if not isinstance(content, str) or not content.strip():
            raise ValueError("LM Studio chat completion returned empty content")
        return content.strip()

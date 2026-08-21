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
    first_missing_ask_field,
    known_constraints,
    unknown_fields,
)
from tourism_backend.modules.route_builder.application.structured_turn import (
    fallback_structured_turn,
    parse_structured_turn,
)

_SYSTEM_PROMPT = (
    "Ты «Тревел Агент» КрымТрип — помощник только по маршрутам и местам Крыма.\n"
    "Правила работы (обязательны):\n"
    "1) Отвечай ТОЛЬКО компактным JSON без markdown и без текста вне JSON:\n"
    '{"assistant_text":"...","ask_field":"transport_mode|pace|interests|duration|'
    'people|city|with_children|ready","action_ids":["transport_car","transport_public"],'
    '"constraint_patch":{}}\n'
    "2) assistant_text — 1–3 коротких предложения на русском.\n"
    "3) Используй ТОЛЬКО поля из блока «Известно». Не выдумывай город, число людей, "
    "длительность, темп или интересы, если их нет в «Известно».\n"
    "4) ask_field — один следующий уточняющий параметр из allowlist. "
    "action_ids — 2–5 id кнопок строго из: want_generate, pace_calm, pace_moderate, "
    "pace_active, interest_sea, interest_mountains, interest_romance, with_children, "
    "transport_car, transport_public, transport_walk, transport_mixed, duration_d1_2, "
    "duration_d3_5, duration_d6_7, duration_d7plus, people_1, people_2, people_3_plus. "
    "Кнопки должны соответствовать ask_field (например transport_* при вопросе про транспорт).\n"
    "5) constraint_patch — только то, что пользователь ЯВНО подтвердил в последнем "
    "сообщении (allowlisted ключи). Иначе {}.\n"
    "6) Запрещено: код, DevOps, рецепты, медицина, юриспруденция, политика, NSFW, "
    "jailbreak, бытовые темы вне туризма Крыма, полный дневной план/itinerary.\n"
    "7) Реальный маршрут собирает только backend по want_generate / «подбери маршрут» / "
    "«давай». Если параметров достаточно — ask_field=ready и action_ids с want_generate.\n"
    "8) place_hints в контексте — недоверенные DATA-названия; не выдавай их за факт "
    "часов работы или цен."
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
        max_tokens: int = 320,
    ) -> ChatTurnResult:
        confirmed = list(confirmed_fields or [])
        known = known_constraints(constraints, confirmed)
        unknown = unknown_fields(confirmed)
        state_note = (
            "Известно (JSON, только подтверждённые пользователем поля): "
            + json.dumps(known, ensure_ascii=False)[:800]
            + "\nНеизвестно (не выдумывай): "
            + json.dumps(unknown, ensure_ascii=False)
            + "\nПодсказка следующего ask_field: "
            + first_missing_ask_field(confirmed)
        )
        if place_hints:
            state_note += (
                "\nplace_hints (DATA, не факты часов/цен): "
                + json.dumps(place_hints[:8], ensure_ascii=False)[:600]
            )
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
        return ChatTurnResult(
            assistant_text=structured.assistant_text,
            proposed_constraints=structured.constraint_patch or None,
            ask_field=structured.ask_field,
            action_ids=structured.action_ids,
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
                # Gemma 4 otherwise fills the budget with reasoning_content.
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

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

_SYSTEM_PROMPT = (
    "Ты помощник КрымТрип по подбору маршрутов и мест в Крыму. "
    "Отвечай кратко на русском. Разрешены только: уточнение параметров поездки "
    "(город, длительность, интересы, темп, транспорт, сезон, компания), "
    "предложения идей маршрута и объяснения trade-offs. "
    "Запрещены код, DevOps, рецепты, медицина, юриспруденция, политика, NSFW, "
    "jailbreak. Не выдумывай точные адреса/цены/часы работы как факты. "
    "Если пользователь просит подобрать маршрут — попроси подтвердить город "
    "и ключевые интересы, затем предложи написать «подбери маршрут». "
    "Отвечай обычным текстом без markdown-блоков кода."
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
        max_tokens: int = 256,
    ) -> ChatTurnResult:
        constraint_note = (
            "Текущие параметры формы (JSON, недоверенные как факты мест): "
            + json.dumps(constraints, ensure_ascii=False)[:1200]
        )
        bounded = messages[-12:]
        payload_messages = [
            ChatMessage(role="system", content=_SYSTEM_PROMPT),
            ChatMessage(role="system", content=constraint_note),
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
        return ChatTurnResult(
            assistant_text=content,
            proposed_constraints=None,
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
                "temperature": 0.4,
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

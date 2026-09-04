"""DeepSeek adapter over its OpenAI-compatible HTTP API.

Документация проверена 2026-09-04 (api-docs.deepseek.com):

* базовый адрес ``https://api.deepseek.com``, метод ``POST /chat/completions``;
* ключ передаётся заголовком ``Authorization: Bearer``;
* модели ``deepseek-v4-flash`` (дешёвая) и ``deepseek-v4-pro``, контекст 1M;
* режим JSON включается ``response_format={"type": "json_object"}`` и требует
  слова «json» в промпте и примера структуры — в CHAT_SYSTEM_PROMPT есть и
  то и другое;
* документация честно предупреждает: «The API may occasionally return empty
  content». Поэтому пустой ответ здесь не исключение уровня 500, а причина
  перейти к следующей модели цепочки, а затем — к fallback-ходу.

Отдельный файл, а не ветка в ``lm_studio.py``: у LM Studio есть локальный
шлюз на одну инференс-сессию (GPU один), у облачного DeepSeek его быть не
должно — иначе два пользователя в чате встанут в очередь друг за другом.
"""

from __future__ import annotations

import json
import time
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
from tourism_backend.modules.route_builder.application.prompts import CHAT_SYSTEM_PROMPT
from tourism_backend.modules.route_builder.application.structured_turn import (
    extract_json_object,
    fallback_structured_turn,
    parse_structured_turn,
)

_DEFAULT_BASE_URL = "https://api.deepseek.com"

# 429 и 5xx переживает другая модель; 400/401/403 — нет, это наш запрос или
# наш ключ, и перебор моделей только потратит время.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# Доля общего таймаута, после которой цепочка моделей не начинает новую
# попытку: лучше отдать fallback-ход, чем держать пользователя до разрыва.
_CHAIN_DEADLINE_FRACTION = 0.75

_CONTENT_DRAFT_SYSTEM_PROMPT = (
    "Ты редактор туристического каталога КрымТрип. Тебе дают название места, его "
    "категории и (опционально) город. Верни ТОЛЬКО компактный json без markdown:\n"
    '{"proposed_slug":"...","short_description":"...","description":"..."}\n'
    "proposed_slug — латиницей, цифры/дефисы, без домена/языка, до 80 символов.\n"
    "short_description — одно предложение на русском, до 200 символов.\n"
    "description — 2-4 предложения на русском, до 600 символов.\n"
    "Пиши только то, что можно вывести из названия/категории/города. НЕ выдумывай "
    "часы работы, цены, координаты, историю, отзывы или факты, которых тебе не дали."
)

_CONTENT_DRAFT_GROUNDED_SYSTEM_PROMPT = (
    "Ты редактор туристического каталога КрымТрип. Тебе дают название места, его "
    "категории, (опционально) город и source_text — фрагмент статьи Wikipedia об "
    "этом месте. Верни ТОЛЬКО компактный json без markdown:\n"
    '{"proposed_slug":"...","short_description":"...","description":"..."}\n'
    "proposed_slug — латиницей, цифры/дефисы, без домена/языка, до 80 символов.\n"
    "short_description — одно живое предложение на русском, до 200 символов.\n"
    "description — перескажи и адаптируй source_text под стиль путеводителя: живо, "
    "без канцелярита и вики-штампов, 4-8 предложений на русском, до 1200 символов.\n"
    "НЕ добавляй фактов (часы работы, цены, даты, события), которых нет в source_text."
)


class _RetryableDeepSeekError(Exception):
    """Сбой, который имеет смысл повторить следующей моделью цепочки."""


def _model_chain(primary: str, fallbacks: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    chain: list[str] = []
    for model in (primary, *fallbacks):
        cleaned = model.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            chain.append(cleaned)
    return tuple(chain)


class DeepSeekProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        fallback_models: tuple[str, ...] = (),
        base_url: str = _DEFAULT_BASE_URL,
        timeout_seconds: float = 60,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("DeepSeek API key must not be empty")
        if not model.strip():
            raise ValueError("DeepSeek model must not be empty")
        self._api_key = api_key
        self._model = model
        self._model_chain = _model_chain(model, fallback_models)
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._chain_deadline = timeout_seconds * _CHAIN_DEADLINE_FRACTION
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=self._timeout,
            transport=self._transport,
        )

    async def probe(self) -> AIProviderProbeResult:
        async with self._client() as client:
            content = await self._complete(
                client,
                messages=[
                    ChatMessage(
                        role="system",
                        content="Верни только компактный json без markdown.",
                    ),
                    ChatMessage(role="user", content='{"status":"ok","language":"ru"}'),
                ],
                max_tokens=80,
                json_mode=True,
            )
        return AIProviderProbeResult(
            provider="deepseek",
            configured_model=self._model,
            available_models=self._model_chain,
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
            ChatMessage(role="system", content=CHAT_SYSTEM_PROMPT),
            ChatMessage(role="system", content=state_note),
            *bounded,
        ]

        content = ""
        try:
            async with self._client() as client:
                content = await self._complete(
                    client,
                    messages=payload_messages,
                    max_tokens=max_tokens,
                    json_mode=True,
                )
        except (httpx.HTTPError, ValueError, _RetryableDeepSeekError):
            # Ход разговора важнее одной неудачной генерации: пользователю
            # отвечает fallback, а не экран ошибки.
            content = ""

        last_user = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        structured = parse_structured_turn(content, confirmed_fields=confirmed) if content else None
        parse_status = "ok"
        if structured is None:
            parse_status = "fallback"
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
            provider="deepseek",
            structured_parse=parse_status,
        )

    async def draft_place_content(
        self,
        *,
        name: str,
        categories: list[str],
        city: str | None,
        source_text: str | None = None,
    ) -> dict[str, Any]:
        """Черновик slug/описаний для одного места.

        Контракт тот же, что у LM Studio: любая ошибка транспорта или разбора
        поднимается наверх, чтобы вызывающий код взял эвристический черновик,
        а не записал в каталог мусор.
        """
        user_payload: dict[str, Any] = {"name": name, "categories": categories[:6], "city": city}
        if source_text:
            user_payload["source_text"] = source_text
        system_prompt = (
            _CONTENT_DRAFT_GROUNDED_SYSTEM_PROMPT if source_text else _CONTENT_DRAFT_SYSTEM_PROMPT
        )
        async with self._client() as client:
            content = await self._complete(
                client,
                messages=[
                    ChatMessage(role="system", content=system_prompt),
                    ChatMessage(
                        role="user", content=json.dumps(user_payload, ensure_ascii=False)
                    ),
                ],
                max_tokens=900 if source_text else 400,
                json_mode=True,
            )
        parsed = extract_json_object(content)
        if parsed is None:
            raise ValueError("DeepSeek content draft returned invalid JSON")
        return {
            "proposed_slug": parsed.get("proposed_slug"),
            "short_description": parsed.get("short_description"),
            "description": parsed.get("description"),
            "provider": "deepseek",
            "model": self._model,
            "prompt_version": "content-v1",
        }

    async def _complete(
        self,
        client: httpx.AsyncClient,
        *,
        messages: list[ChatMessage],
        max_tokens: int,
        json_mode: bool = False,
    ) -> str:
        started = time.monotonic()
        last_error: Exception | None = None
        for index, model in enumerate(self._model_chain):
            if index > 0 and time.monotonic() - started > self._chain_deadline:
                break
            try:
                return await self._complete_with(
                    client,
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                )
            except (_RetryableDeepSeekError, httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise ValueError("DeepSeek request failed on every model in the chain") from last_error
        raise ValueError("DeepSeek model chain is empty")

    async def _complete_with(
        self,
        client: httpx.AsyncClient,
        *,
        model: str,
        messages: list[ChatMessage],
        max_tokens: int,
        json_mode: bool,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            response = await client.post("/chat/completions", json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in _RETRYABLE_STATUSES:
                raise _RetryableDeepSeekError(f"status {status}") from exc
            # Тело ответа не логируем и не пробрасываем: в запросе едет ключ.
            raise ValueError(f"DeepSeek request failed with status {status}") from exc

        body: Any = response.json()
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("DeepSeek chat completion returned an invalid payload") from exc
        if not isinstance(content, str) or not content.strip():
            # Известное поведение API: изредка приходит пустой content.
            # Для нас это повод попробовать следующую модель, а не ошибка.
            raise _RetryableDeepSeekError("empty content")
        return content.strip()

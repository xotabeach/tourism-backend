"""Gemini adapter over Google's Generative Language REST API.

Cloud counterpart to ``lm_studio.py``: same ``AIPlanningProvider`` contract,
same shared system prompt (``prompts.CHAT_SYSTEM_PROMPT``) and the same
downstream structured-turn parsing, so an admin switching
``ai_provider`` between ``lmstudio`` and ``gemini`` changes only which
model answers, not the conversation contract. Unlike LM Studio there is no
single-GPU busy slot to serialize — Gemini is a hosted API and can serve
concurrent requests, so no ``acquire_inference_slot`` equivalent here.
"""

from __future__ import annotations

import json
import logging
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
    fallback_structured_turn,
    parse_structured_turn,
)

_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# Our ChatMessage.role vocabulary is system/user/assistant; Gemini's
# `contents` array only accepts user/model turns (system-level text goes in
# `systemInstruction` instead), so "system" is intentionally absent here.
_ROLE_MAP = {"user": "user", "assistant": "model"}

_logger = logging.getLogger("tourism_backend.gemini")


def _model_chain(primary: str, fallbacks: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    chain: list[str] = []
    for model in (primary, *fallbacks):
        cleaned = model.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            chain.append(cleaned)
    return tuple(chain)


class GeminiProvider:
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
            raise ValueError("Gemini API key must not be empty")
        if not model.strip():
            raise ValueError("Gemini model must not be empty")
        self._api_key = api_key
        self._model = model
        # Preferred model first, then older/cheaper models to fall back to
        # when the preferred one is rate-limited — see `_generate`.
        self._model_chain = _model_chain(model, fallback_models)
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport

    async def probe(self) -> AIProviderProbeResult:
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            content = await self._generate(
                client,
                system_instruction="Верни только компактный JSON без markdown.",
                contents=[{"role": "user", "parts": [{"text": '{"status":"ok","language":"ru"}'}]}],
                max_output_tokens=80,
            )
        # Gemini has no cheap "list loaded models" equivalent to LM Studio's
        # /v1/models — a successful generateContent call against the
        # configured model (or its fallback chain) is itself the check.
        return AIProviderProbeResult(
            provider="gemini",
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
        system_instruction = f"{CHAT_SYSTEM_PROMPT}\n\n{state_note}"

        bounded = messages[-12:]
        contents = [
            {"role": _ROLE_MAP[message.role], "parts": [{"text": message.content}]}
            for message in bounded
            if message.role in _ROLE_MAP
        ]
        if not contents:
            # Gemini requires at least one turn in `contents`; a fresh chat
            # with only system-level state (no user message yet) still
            # needs somewhere to anchor the request.
            contents = [{"role": "user", "parts": [{"text": "Начни диалог."}]}]

        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            content = await self._generate(
                client,
                system_instruction=system_instruction,
                contents=contents,
                max_output_tokens=max_tokens,
            )

        last_user = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        structured = parse_structured_turn(content, confirmed_fields=confirmed)
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
            provider="gemini",
            structured_parse=parse_status,
        )

    async def _generate(
        self,
        client: httpx.AsyncClient,
        *,
        system_instruction: str,
        contents: list[dict[str, Any]],
        max_output_tokens: int,
    ) -> str:
        """Try the preferred model, then fall back on rate limiting only.

        A 429 on the preferred model moves to the next model in the chain —
        the exact "быстро переключаться" behaviour requested — but any other
        failure (bad request, server error, timeout) raises immediately
        instead of burning through the rest of the chain for an error that
        switching models will not fix.
        """
        last_status: int | None = None
        for index, model in enumerate(self._model_chain):
            url = f"{self._base_url}/models/{model}:generateContent"
            try:
                response = await client.post(
                    url,
                    params={"key": self._api_key},
                    json={
                        "contents": contents,
                        "systemInstruction": {"parts": [{"text": system_instruction}]},
                        "generationConfig": {
                            "maxOutputTokens": max_output_tokens,
                            "temperature": 0.3,
                        },
                    },
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                has_next = index + 1 < len(self._model_chain)
                if status == 429 and has_next:
                    last_status = status
                    _logger.warning(
                        "gemini_model_rate_limited_falling_back",
                        extra={"model": model, "next_model": self._model_chain[index + 1]},
                    )
                    continue
                # Never echo the request (the API key rides in its `?key=`
                # query param) — only the status code is safe to surface.
                raise ValueError(f"Gemini request failed with status {status}") from exc
            payload: Any = response.json()
            try:
                text = payload["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError, TypeError) as exc:
                raise ValueError("Gemini returned an invalid payload") from exc
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Gemini returned empty content")
            if index > 0:
                _logger.info("gemini_model_fallback_served_request", extra={"model": model})
            return text.strip()
        raise ValueError(f"Gemini request failed with status {last_status}")

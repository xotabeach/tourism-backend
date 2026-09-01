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
    fallback_structured_turn,
    parse_structured_turn,
)

_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# Our ChatMessage.role vocabulary is system/user/assistant; Gemini's
# `contents` array only accepts user/model turns (system-level text goes in
# `systemInstruction` instead), so "system" is intentionally absent here.
_ROLE_MAP = {"user": "user", "assistant": "model"}

_logger = logging.getLogger("tourism_backend.gemini")

# Failures another model in the chain could plausibly survive. 503 in
# particular is Gemini's "this model is currently experiencing high demand"
# and was observed rolling across the newest models minute-to-minute while
# an older one answered in ~2s, so treating only 429 as retryable took the
# whole chat down for a failure the chain exists to absorb.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# Gemini 3.x flash models are *thinking* models: reasoning tokens are spent
# from the same `maxOutputTokens` budget as the answer, and thinking cannot
# be disabled on them (`thinkingConfig.thinkingBudget: 0` is rejected with
# 400 INVALID_ARGUMENT). Verified live: an 40-token cap returned an empty
# `content` with `finishReason: MAX_TOKENS`, while the same prompt with room
# to think answered normally after spending ~120-200 tokens on thoughts. The
# caller's allowance therefore gets headroom on top rather than being
# silently consumed. This is a cap, not a spend — a model that finishes
# thinking early never bills the rest.
_THINKING_TOKEN_HEADROOM = 2048

# A model that hangs must not eat the whole request budget. The mobile
# client gives the whole turn 20s (Dio receiveTimeout) before it reports
# "Network request failed", so the entire walk has to fit inside that.
#
# Each attempt therefore gets an equal share of what is left rather than a
# fixed slice: with a fixed 7s slot and two hanging models the budget ran
# out before the walk ever reached a model that answers, so the first
# request after a restart failed every time (measured on the server: 14.6s
# then an error, while later requests took 3.5s because the breaker had
# learned which models were down). Sharing the remainder keeps early rungs
# short — they are cheap probes of the preferred model — while the last
# rung still gets a usable slice, since it is the one that has to answer.
# Healthy single-model replies measured 1.2-5.7s, so the floor stays above
# that; the ceiling keeps one hanging model from eating a large budget.
_MIN_ATTEMPT_TIMEOUT_SECONDS = 3.5
_MAX_ATTEMPT_TIMEOUT_SECONDS = 8.0
# Stop starting new attempts once this much of the caller's budget is gone,
# so the walk fails with a real error instead of the client timing out.
_CHAIN_DEADLINE_FRACTION = 0.9

# Per-model breaker, same shape and rationale as two_gis_tsp.py's: while a
# model is down, paying its timeout on every single request would make the
# newest-model-first preference cost every user 7s for nothing. One request
# per cooldown pays the probe; the rest skip straight to a model that works.
_CIRCUIT_FAILURE_THRESHOLD = 2
_CIRCUIT_COOLDOWN_SECONDS = 120.0


class _RetryableGeminiError(Exception):
    """A failure worth re-trying on the next model rather than surfacing."""


class _ModelCircuitBreaker:
    """Process-wide health memory keyed by model name."""

    def __init__(self) -> None:
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    def is_open(self, model: str) -> bool:
        opened = self._opened_at.get(model)
        if opened is None:
            return False
        if time.monotonic() - opened < _CIRCUIT_COOLDOWN_SECONDS:
            return True
        # Cooldown elapsed — let the next request probe it again.
        self._opened_at.pop(model, None)
        self._failures.pop(model, None)
        return False

    def record_success(self, model: str) -> None:
        self._failures.pop(model, None)
        self._opened_at.pop(model, None)

    def record_failure(self, model: str) -> None:
        count = self._failures.get(model, 0) + 1
        self._failures[model] = count
        if count >= _CIRCUIT_FAILURE_THRESHOLD:
            self._opened_at[model] = time.monotonic()


_circuit = _ModelCircuitBreaker()


def reset_gemini_circuit_for_tests() -> None:
    global _circuit
    _circuit = _ModelCircuitBreaker()


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
        self._chain_deadline = timeout_seconds * _CHAIN_DEADLINE_FRACTION
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
        """Try the preferred model, moving on whenever another model could help.

        Implements the "быстро переключаться на другую модель" requirement.
        A permanent, model-independent failure (401/403/404/400) still raises
        straight away — walking the rest of the chain would only repeat it —
        but every transient shape (rate limit, 5xx, timeout, and a turn whose
        whole budget went into thinking) advances to the next rung.

        Two things keep "prefer the newest model" from costing every caller
        the newest model's downtime: a model that has just failed twice is
        skipped for a cooldown, and the walk stops starting new attempts once
        the caller's budget is nearly spent, so the client gets an error from
        us rather than a timeout of its own.
        """
        started = time.monotonic()
        candidates = [model for model in self._model_chain if not _circuit.is_open(model)]
        if not candidates:
            # Everything is in cooldown: rather than fail without trying,
            # spend the budget on the preferred model.
            candidates = [self._model_chain[0]]

        last_error = "no model attempted"
        for index, model in enumerate(candidates):
            remaining = self._chain_deadline - (time.monotonic() - started)
            if index > 0 and remaining <= 0:
                last_error = f"{last_error} (chain deadline reached)"
                break
            # Split what is left evenly across the models still to try, so a
            # hanging rung cannot starve the ones behind it.
            attempt_seconds = min(
                _MAX_ATTEMPT_TIMEOUT_SECONDS,
                max(_MIN_ATTEMPT_TIMEOUT_SECONDS, remaining / (len(candidates) - index)),
            )
            try:
                text = await self._generate_once(
                    client,
                    model=model,
                    system_instruction=system_instruction,
                    contents=contents,
                    max_output_tokens=max_output_tokens,
                    attempt_seconds=attempt_seconds,
                )
            except _RetryableGeminiError as exc:
                _circuit.record_failure(model)
                last_error = str(exc)
                if index + 1 >= len(candidates):
                    break
                _logger.warning(
                    "gemini_model_unavailable_falling_back",
                    extra={
                        "model": model,
                        "next_model": candidates[index + 1],
                        "reason": last_error,
                    },
                )
                continue
            _circuit.record_success(model)
            if model != self._model_chain[0]:
                _logger.info("gemini_model_fallback_served_request", extra={"model": model})
            return text
        raise ValueError(f"Gemini request failed: {last_error}")

    async def _generate_once(
        self,
        client: httpx.AsyncClient,
        *,
        model: str,
        system_instruction: str,
        contents: list[dict[str, Any]],
        max_output_tokens: int,
        attempt_seconds: float,
    ) -> str:
        url = f"{self._base_url}/models/{model}:generateContent"
        try:
            response = await client.post(
                url,
                # The key goes in a header, never the `?key=` query param the
                # API also accepts: httpx logs every request URL at INFO and
                # the service runs at LOG_LEVEL=INFO, so the query-param form
                # wrote the raw API key into container logs (and would leak it
                # into any proxy log or URL-bearing error message too).
                headers={"x-goog-api-key": self._api_key},
                json={
                    "contents": contents,
                    "systemInstruction": {"parts": [{"text": system_instruction}]},
                    "generationConfig": {
                        "maxOutputTokens": max_output_tokens + _THINKING_TOKEN_HEADROOM,
                        "temperature": 0.3,
                    },
                },
                timeout=httpx.Timeout(attempt_seconds),
            )
            response.raise_for_status()
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            raise _RetryableGeminiError(f"{type(exc).__name__} on {model}") from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in _RETRYABLE_STATUSES:
                raise _RetryableGeminiError(f"status {status}") from exc
            # Never echo the request (the API key rides in its `?key=`
            # query param) — only the status code is safe to surface.
            raise ValueError(f"Gemini request failed with status {status}") from exc

        payload: Any = response.json()
        try:
            candidate = payload["candidates"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Gemini returned an invalid payload") from exc
        if not isinstance(candidate, dict):
            raise ValueError("Gemini returned an invalid payload")
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not parts:
            if candidate.get("finishReason") == "MAX_TOKENS":
                # Reasoning burned the entire budget before a single answer
                # token. A model that thinks less may still answer this turn.
                raise _RetryableGeminiError(f"{model} spent the whole budget on thinking")
            raise ValueError("Gemini returned an invalid payload")
        first = parts[0]
        text = first.get("text") if isinstance(first, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Gemini returned empty content")
        return text.strip()

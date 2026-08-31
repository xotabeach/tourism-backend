"""Contract tests for the Gemini adapter (Workstream B)."""

from __future__ import annotations

import json

import httpx
import pytest

from tourism_backend.config import AIProvider, Settings, validate_settings
from tourism_backend.modules.route_builder.application.ai import ChatMessage
from tourism_backend.modules.route_builder.infrastructure.gemini import GeminiProvider


def _provider(handler: httpx.MockTransport) -> GeminiProvider:
    return GeminiProvider(
        api_key="secret-token",
        model="gemini-test",
        timeout_seconds=5,
        transport=handler,
    )


@pytest.mark.asyncio
async def test_probe_checks_the_configured_model_via_generate_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1beta/models/gemini-test:generateContent"
        assert request.url.params["key"] == "secret-token"
        body = json.loads(request.content)
        assert body["contents"][0]["role"] == "user"
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": '{"status":"ok"}'}]}}]},
        )

    result = await _provider(httpx.MockTransport(handler)).probe()

    assert result.provider == "gemini"
    assert result.configured_model == "gemini-test"
    assert result.available_models == ("gemini-test",)
    assert result.response_text == '{"status":"ok"}'


@pytest.mark.asyncio
async def test_chat_turn_maps_roles_and_moves_system_state_out_of_contents() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # No "system" role reaches `contents` — Gemini only accepts user/model
        # there; our system prompt + state note must land in systemInstruction.
        assert {item["role"] for item in body["contents"]} <= {"user", "model"}
        assert "Известно" in body["systemInstruction"]["parts"][0]["text"]
        assert body["contents"][-1]["role"] == "user"
        assert body["contents"][-1]["parts"][0]["text"] == "Хочу маршрут по Ялте"
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "assistant_text": "Уточните длительность поездки.",
                                            "ask_field": "duration",
                                            "action_ids": ["duration_d1_2"],
                                            "constraint_patch": {},
                                        },
                                        ensure_ascii=False,
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        )

    result = await _provider(httpx.MockTransport(handler)).chat_turn(
        messages=[ChatMessage(role="user", content="Хочу маршрут по Ялте")],
        constraints={"city": "Ялта"},
        confirmed_fields=["city"],
    )

    assert result.provider == "gemini"
    assert result.structured_parse == "ok"
    assert result.assistant_text == "Уточните длительность поездки."
    assert result.ask_field == "duration"


@pytest.mark.asyncio
async def test_non_json_reply_falls_back_instead_of_raising() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "не структурированный ответ"}]}}]},
        )

    result = await _provider(httpx.MockTransport(handler)).chat_turn(
        messages=[ChatMessage(role="user", content="привет")],
        constraints={},
        confirmed_fields=[],
    )

    assert result.structured_parse == "fallback"
    assert result.provider == "gemini"


@pytest.mark.asyncio
async def test_assistant_history_maps_to_model_role() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        roles = [item["role"] for item in body["contents"]]
        assert roles == ["user", "model", "user"]
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": '{"assistant_text":"ok"}'}]}}]},
        )

    await _provider(httpx.MockTransport(handler)).chat_turn(
        messages=[
            ChatMessage(role="user", content="привет"),
            ChatMessage(role="assistant", content="привет! куда едем?"),
            ChatMessage(role="user", content="в Ялту"),
        ],
        constraints={},
        confirmed_fields=[],
    )


@pytest.mark.asyncio
async def test_http_failure_never_leaks_the_api_key() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "denied"}})

    with pytest.raises(ValueError, match="403") as error:
        await _provider(httpx.MockTransport(handler)).probe()

    assert "secret-token" not in str(error.value)


def test_gemini_provider_requires_the_api_key_when_ai_planning_enabled() -> None:
    # _env_file=None: isolate from a real developer .env, which may already
    # define GEMINI_API_KEY and would otherwise mask this check. The model
    # itself needs no explicit config — gemini_model has a working default.
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        validate_settings(
            Settings(  # type: ignore[call-arg]
                _env_file=None,
                ai_planning_enabled=True,
                ai_provider=AIProvider.GEMINI,
            )
        )


@pytest.mark.asyncio
async def test_falls_back_to_the_next_model_on_rate_limit() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("gemini-primary:generateContent"):
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        assert request.url.path.endswith("gemini-fallback:generateContent")
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": '{"assistant_text":"ok"}'}]}}]},
        )

    provider = GeminiProvider(
        api_key="secret-token",
        model="gemini-primary",
        fallback_models=("gemini-fallback",),
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )

    result = await provider.chat_turn(
        messages=[ChatMessage(role="user", content="привет")],
        constraints={},
        confirmed_fields=[],
    )

    assert len(calls) == 2
    assert result.assistant_text == "ok"


@pytest.mark.asyncio
async def test_non_rate_limit_error_never_cascades_to_the_next_model() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(500, json={"error": {"message": "boom"}})

    provider = GeminiProvider(
        api_key="secret-token",
        model="gemini-primary",
        fallback_models=("gemini-fallback",),
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ValueError, match="500"):
        await provider.probe()

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_rate_limit_on_every_model_raises_after_trying_them_all() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    provider = GeminiProvider(
        api_key="secret-token",
        model="gemini-primary",
        fallback_models=("gemini-fallback",),
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ValueError, match="429"):
        await provider.probe()

    assert len(calls) == 2


def test_duplicate_and_blank_fallback_entries_are_ignored() -> None:
    provider = GeminiProvider(
        api_key="secret-token",
        model="gemini-primary",
        fallback_models=("gemini-primary", "", "  ", "gemini-fallback"),
    )

    assert provider._model_chain == ("gemini-primary", "gemini-fallback")

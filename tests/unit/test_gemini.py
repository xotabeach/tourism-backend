"""Contract tests for the Gemini adapter (Workstream B)."""

from __future__ import annotations

import json

import httpx
import pytest

from tourism_backend.config import AIProvider, Settings, validate_settings
from tourism_backend.modules.route_builder.application.ai import ChatMessage
from tourism_backend.modules.route_builder.infrastructure.gemini import (
    GeminiProvider,
    reset_gemini_circuit_for_tests,
)


@pytest.fixture(autouse=True)
def _clean_circuit() -> None:
    """The breaker is process-wide, so one test's failures must not skip
    another test's models."""
    reset_gemini_circuit_for_tests()


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
        # The key must ride in a header, never the URL — httpx logs request
        # URLs at INFO, which is the level the service actually runs at.
        assert request.headers["x-goog-api-key"] == "secret-token"
        assert "key" not in request.url.params
        assert "secret-token" not in str(request.url)
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


@pytest.mark.asyncio
async def test_api_key_never_appears_in_the_request_url() -> None:
    """Regression: the key used to ride in `?key=`, which httpx logs at INFO."""
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": '{"assistant_text":"ok"}'}]}}]},
        )

    await _provider(httpx.MockTransport(handler)).chat_turn(
        messages=[ChatMessage(role="user", content="привет")],
        constraints={},
        confirmed_fields=[],
    )

    assert urls
    assert all("secret-token" not in url for url in urls)


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
async def test_permanent_error_never_cascades_to_the_next_model() -> None:
    """A 400 is the same on every model — burning the chain on it is waste."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    provider = GeminiProvider(
        api_key="secret-token",
        model="gemini-primary",
        fallback_models=("gemini-fallback",),
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ValueError, match="400"):
        await provider.probe()

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_overloaded_model_falls_back_instead_of_failing_the_turn() -> None:
    """503 "high demand" is what the newest models actually answer under load."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("gemini-primary:generateContent"):
            return httpx.Response(503, json={"error": {"message": "high demand"}})
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
async def test_hanging_model_falls_back_instead_of_failing_the_turn() -> None:
    """The newest model was observed hanging outright, not just erroring."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("gemini-primary:generateContent"):
            raise httpx.ReadTimeout("timed out", request=request)
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
async def test_budget_spent_on_thinking_falls_back_to_a_model_that_thinks_less() -> None:
    """Thinking models can return finishReason=MAX_TOKENS with no content."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("gemini-primary:generateContent"):
            return httpx.Response(
                200,
                json={"candidates": [{"content": {}, "finishReason": "MAX_TOKENS", "index": 0}]},
            )
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
async def test_request_reserves_headroom_for_thinking_tokens() -> None:
    """Reasoning is billed from maxOutputTokens, so the answer needs its own room."""
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["generationConfig"]["maxOutputTokens"])
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": '{"assistant_text":"ok"}'}]}}]},
        )

    await _provider(httpx.MockTransport(handler)).chat_turn(
        messages=[ChatMessage(role="user", content="привет")],
        constraints={},
        confirmed_fields=[],
        max_tokens=360,
    )

    assert seen
    assert seen[0] > 360


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


@pytest.mark.asyncio
async def test_a_failing_model_is_skipped_on_the_next_request() -> None:
    """The mobile client gives the whole turn 20s, so paying a dead model's
    timeout on every single request is what made the chat unusable."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("gemini-primary:generateContent"):
            return httpx.Response(503, json={"error": {"message": "high demand"}})
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

    for _ in range(3):
        result = await provider.chat_turn(
            messages=[ChatMessage(role="user", content="привет")],
            constraints={},
            confirmed_fields=[],
        )
        assert result.assistant_text == "ok"

    primary_attempts = [path for path in calls if path.endswith("gemini-primary:generateContent")]
    # Two strikes open the breaker, so the third turn skips the dead model
    # entirely rather than waiting on it again.
    assert len(primary_attempts) == 2
    assert len(calls) == 5


@pytest.mark.asyncio
async def test_the_breaker_never_leaves_the_chain_empty() -> None:
    """With every model in cooldown the preferred one is still attempted —
    failing without trying would turn a recovered outage into a dead chat."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) <= 4:
            return httpx.Response(503, json={"error": {"message": "high demand"}})
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

    for _ in range(2):
        with pytest.raises(ValueError, match="503"):
            await provider.probe()

    recovered = await provider.probe()
    assert recovered.response_text == '{"assistant_text":"ok"}'
    assert calls[-1].endswith("gemini-primary:generateContent")

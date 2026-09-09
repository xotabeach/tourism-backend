"""DeepSeek-провайдер.

Проверяем то, что отличает его от LM Studio: цепочку моделей на 429/5xx,
режим json_object и обработку пустого content — документация DeepSeek прямо
предупреждает, что такое иногда приходит.
"""

import json

import httpx
import pytest

from tourism_backend.config import AIProvider, Settings, validate_settings
from tourism_backend.modules.route_builder.application.ai import ChatMessage
from tourism_backend.modules.route_builder.infrastructure.deepseek import DeepSeekProvider


def _provider(handler: httpx.MockTransport, **kwargs: object) -> DeepSeekProvider:
    return DeepSeekProvider(
        api_key="secret-token",
        model="deepseek-v4-flash",
        fallback_models=("deepseek-v4-pro",),
        base_url="https://api.deepseek.test",
        timeout_seconds=5,
        transport=handler,
        **kwargs,  # type: ignore[arg-type]
    )


def _turn_body(text: str) -> dict[str, object]:
    return {"choices": [{"message": {"content": text}}]}


@pytest.mark.asyncio
async def test_probe_sends_a_bearer_key_and_asks_for_json() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer secret-token"
        assert request.url.path == "/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "deepseek-v4-flash"
        assert body["stream"] is False
        assert body["response_format"] == {"type": "json_object"}
        assert body["thinking"] == {"type": "disabled"}
        return httpx.Response(200, json=_turn_body('{"status":"ok"}'))

    result = await _provider(httpx.MockTransport(handler)).probe()

    assert result.provider == "deepseek"
    assert result.configured_model == "deepseek-v4-flash"
    assert result.available_models == ("deepseek-v4-flash", "deepseek-v4-pro")
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_a_rate_limited_model_falls_through_to_the_next_one() -> None:
    models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        models.append(model)
        if model == "deepseek-v4-flash":
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json=_turn_body('{"status":"ok"}'))

    result = await _provider(httpx.MockTransport(handler)).probe()

    assert models == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert result.response_text == '{"status":"ok"}'


@pytest.mark.asyncio
async def test_an_empty_answer_is_retried_on_the_next_model() -> None:
    """«The API may occasionally return empty content» — документация DeepSeek."""
    models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        models.append(model)
        if model == "deepseek-v4-flash":
            return httpx.Response(200, json=_turn_body("   "))
        return httpx.Response(200, json=_turn_body('{"status":"ok"}'))

    result = await _provider(httpx.MockTransport(handler)).probe()

    assert models == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert result.response_text == '{"status":"ok"}'


@pytest.mark.asyncio
async def test_a_bad_key_is_not_retried_and_never_echoes_the_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": {"message": "Authentication Fails"}})

    with pytest.raises(ValueError, match="401") as error:
        await _provider(httpx.MockTransport(handler)).probe()

    assert calls == 1
    assert "401" in str(error.value)
    assert "secret-token" not in str(error.value)


@pytest.mark.asyncio
async def test_chat_turn_falls_back_instead_of_failing_the_conversation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    result = await _provider(httpx.MockTransport(handler)).chat_turn(
        messages=[ChatMessage(role="user", content="Хочу маршрут на день у моря")],
        constraints={},
    )

    # Пользователь получает ход разговора, а не экран ошибки.
    assert result.provider == "deepseek"
    assert result.structured_parse == "fallback"
    assert result.assistant_text


@pytest.mark.asyncio
async def test_chat_turn_reads_a_structured_answer() -> None:
    payload = {
        "assistant_text": "Понял, ищу морской маршрут на день.",
        "ask_field": "ready",
        "constraint_patch": {"duration_hours": 6},
        "action_ids": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_turn_body(json.dumps(payload, ensure_ascii=False)))

    result = await _provider(httpx.MockTransport(handler)).chat_turn(
        messages=[ChatMessage(role="user", content="Хочу маршрут на день у моря")],
        constraints={},
    )

    assert result.structured_parse == "ok"
    assert result.assistant_text == "Понял, ищу морской маршрут на день."


def test_settings_require_a_key_when_deepseek_is_selected() -> None:
    with pytest.raises(RuntimeError) as error:
        validate_settings(
            Settings(
                ai_provider=AIProvider.DEEPSEEK,
                ai_planning_enabled=True,
                jwt_signing_key="test-jwt-signing-key-at-least-32-chars!!",
                # Explicit: Settings otherwise reads the developer's own .env,
                # so a real key sitting there made the missing-key case
                # impossible to construct and this test failed only on that
                # machine.
                deepseek_api_key=None,
            )
        )
    assert "DEEPSEEK_API_KEY" in str(error.value)

import json

import httpx
import pytest

from tourism_backend.config import Settings, validate_settings
from tourism_backend.modules.route_builder.application.ai import ChatMessage
from tourism_backend.modules.route_builder.infrastructure.lm_studio import LMStudioProvider


def _provider(handler: httpx.MockTransport) -> LMStudioProvider:
    return LMStudioProvider(
        base_url="http://lm-studio.test/v1",
        model="gemma-test",
        api_key="secret-token",
        timeout_seconds=5,
        transport=handler,
    )


@pytest.mark.asyncio
async def test_lm_studio_probe_checks_model_and_completion() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer secret-token"
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gemma-test"}]})
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "gemma-test"
        assert body["stream"] is False
        assert body["reasoning_effort"] == "none"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"status":"ok"}'}}]},
        )

    result = await _provider(httpx.MockTransport(handler)).probe()

    assert result.provider == "lmstudio"
    assert result.configured_model == "gemma-test"
    assert result.available_models == ("gemma-test",)
    assert result.response_text == '{"status":"ok"}'
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_lm_studio_chat_turn_sends_reasoning_effort_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["reasoning_effort"] == "none"
        assert body["model"] == "gemma-test"
        assert body["messages"][0]["role"] == "system"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Уточните длительность поездки."}}]},
        )

    result = await _provider(httpx.MockTransport(handler)).chat_turn(
        messages=[ChatMessage(role="user", content="Хочу спокойный день")],
        constraints={"city": "Ялта"},
    )
    assert result.provider == "lmstudio"
    assert "длительность" in result.assistant_text


@pytest.mark.asyncio
async def test_lm_studio_probe_rejects_unloaded_configured_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "another-model"}]})

    with pytest.raises(ValueError, match="is not loaded"):
        await _provider(httpx.MockTransport(handler)).probe()


@pytest.mark.asyncio
async def test_lm_studio_probe_rejects_malformed_completion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gemma-test"}]})
        return httpx.Response(200, json={"choices": []})

    with pytest.raises(ValueError, match="invalid payload"):
        await _provider(httpx.MockTransport(handler)).probe()


def test_enabled_lm_studio_requires_endpoint_and_model() -> None:
    settings = Settings(ai_planning_enabled=True, ai_provider="lmstudio")

    with pytest.raises(RuntimeError, match="LM_STUDIO_BASE_URL"):
        validate_settings(settings)


def test_enabled_lm_studio_accepts_private_http_endpoint() -> None:
    settings = Settings(
        ai_planning_enabled=True,
        ai_provider="lmstudio",
        lm_studio_base_url="http://100.64.0.10:1234/v1",
        lm_studio_model="gemma-test",
    )

    validate_settings(settings)


def test_enabled_lm_studio_rejects_unsupported_url_scheme() -> None:
    settings = Settings(
        ai_planning_enabled=True,
        ai_provider="lmstudio",
        lm_studio_base_url="file:///tmp/model",
        lm_studio_model="gemma-test",
    )

    with pytest.raises(RuntimeError, match="http"):
        validate_settings(settings)

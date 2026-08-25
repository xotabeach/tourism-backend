import json

import httpx
import pytest

from tourism_backend.config import Settings, validate_settings
from tourism_backend.modules.route_builder.application.ai import AIProviderBusyError, ChatMessage
from tourism_backend.modules.route_builder.infrastructure.lm_studio import (
    LMStudioProvider,
    acquire_inference_slot,
    release_inference_slot,
    reset_inference_gate_for_tests,
)


def _provider(handler: httpx.MockTransport) -> LMStudioProvider:
    return LMStudioProvider(
        base_url="http://lm-studio.test/v1",
        model="gemma-test",
        api_key="secret-token",
        timeout_seconds=5,
        transport=handler,
    )


@pytest.fixture(autouse=True)
def _reset_lm_studio_gate() -> None:
    reset_inference_gate_for_tests()
    yield
    reset_inference_gate_for_tests()


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
        assert any("Известно" in message["content"] for message in body["messages"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "assistant_text": "Уточните длительность поездки.",
                                    "ask_field": "duration",
                                    "action_ids": ["duration_d1_2", "duration_d3_5"],
                                    "constraint_patch": {},
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    result = await _provider(httpx.MockTransport(handler)).chat_turn(
        messages=[ChatMessage(role="user", content="Хочу спокойный день")],
        constraints={"city": "Ялта"},
        confirmed_fields=["city"],
    )
    assert result.provider == "lmstudio"
    assert "длительность" in result.assistant_text
    assert result.ask_field == "duration"
    assert "duration_d1_2" in result.action_ids
    assert result.structured_parse == "ok"


@pytest.mark.asyncio
async def test_lm_studio_chat_turn_fails_fast_when_slot_busy() -> None:
    """Second in-process caller must not wait on the 60s HTTP timeout."""
    await acquire_inference_slot()
    try:
        with pytest.raises(AIProviderBusyError, match="lm_studio_busy"):
            await _provider(httpx.MockTransport(lambda _request: httpx.Response(500))).chat_turn(
                messages=[ChatMessage(role="user", content="Ялта")],
                constraints={"city": "Ялта"},
                confirmed_fields=["city"],
            )
    finally:
        await release_inference_slot()


@pytest.mark.asyncio
async def test_lm_studio_chat_turn_marks_unparseable_json_as_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "не JSON, просто текст"}}]},
        )

    result = await _provider(httpx.MockTransport(handler)).chat_turn(
        messages=[ChatMessage(role="user", content="Хочу спокойный день")],
        constraints={"city": "Ялта"},
        confirmed_fields=["city"],
    )
    assert result.structured_parse == "fallback"
    assert result.provider == "lmstudio"


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


@pytest.mark.asyncio
async def test_lm_studio_draft_place_content_parses_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["messages"][0]["role"] == "system"
        user_payload = json.loads(body["messages"][1]["content"])
        assert user_payload == {
            "name": "Ласточкино гнездо",
            "categories": ["Замок"],
            "city": "Ялта",
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "proposed_slug": "lastochkino-gnezdo",
                                    "short_description": "Замок над морем в Ялте.",
                                    "description": "Ласточкино гнездо — визитная "
                                    "карточка Крыма, замок на скале над морем в Ялте.",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    draft = await _provider(httpx.MockTransport(handler)).draft_place_content(
        name="Ласточкино гнездо",
        categories=["Замок"],
        city="Ялта",
    )
    assert draft["proposed_slug"] == "lastochkino-gnezdo"
    assert draft["short_description"] == "Замок над морем в Ялте."
    assert draft["provider"] == "lmstudio"
    assert draft["model"] == "gemma-test"


@pytest.mark.asyncio
async def test_lm_studio_draft_place_content_rejects_non_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "не могу помочь"}}]},
        )

    with pytest.raises(ValueError, match="invalid JSON"):
        await _provider(httpx.MockTransport(handler)).draft_place_content(
            name="Тестовое место",
            categories=[],
            city=None,
        )


def test_enabled_lm_studio_requires_endpoint_and_model() -> None:
    # Explicit None, not just an omitted kwarg: Settings also reads .env, and
    # local dev now configures LM_STUDIO_BASE_URL/LM_STUDIO_MODEL there for
    # the home-lab Gemma box — this test asserts the missing-config case, so
    # it must not depend on the developer's ambient .env being empty.
    settings = Settings(
        ai_planning_enabled=True,
        ai_provider="lmstudio",
        lm_studio_base_url=None,
        lm_studio_model=None,
    )

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

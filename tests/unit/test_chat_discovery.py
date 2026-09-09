"""Broad travel ideas are not a mandatory departure-city questionnaire."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.config import Settings
from tourism_backend.modules.route_builder.application import session_service as service
from tourism_backend.modules.route_builder.application.ai import ChatMessage, ChatTurnResult
from tourism_backend.modules.route_builder.application.chat_context import planning_state_note
from tourism_backend.modules.route_builder.application.discovery import SOUTH_COAST, discovery_patch
from tourism_backend.modules.route_builder.application.structured_turn import parse_structured_turn
from tourism_backend.modules.route_builder.infrastructure.deepseek import DeepSeekProvider
from tourism_backend.modules.route_builder.infrastructure.gemini import GeminiProvider
from tourism_backend.modules.route_builder.infrastructure.lm_studio import LMStudioProvider

REQUEST = (
    "привет! сможешь мне подобрать маршруты вдоль южного берега? "
    "мне очень нравятся поселки по типу Фороса, Симеиза"
)


def test_screenshot_destination_is_not_a_departure_city() -> None:
    assert discovery_patch(REQUEST) == {
        "search_area": SOUTH_COAST,
        "preferred_localities": ["Форос", "Симеиз"],
    }


def test_screenshot_followup_keeps_previous_area() -> None:
    patch = discovery_patch(
        "какой город старта? не нужен мне он, просто хочу начать откуда то "
        "на южном берегу Крыма. ты же гид, предлагай"
    )
    assert patch == {"flexible_start": True}


@pytest.mark.parametrize(
    "text",
    [
        "Не хочу Ялту, не предлагай маршруты там",
        "Не нужен отель",
        "Кроме Фороса",
        "Расскажи, пожалуйста, историю основания города Ялта",
    ],
)
def test_negation_and_factual_questions_are_not_guessed(text: str) -> None:
    assert discovery_patch(text) == {}


def test_provider_can_extract_other_areas_without_changing_interests() -> None:
    turn = parse_structured_turn(
        json.dumps(
            {
                "assistant_text": "Посмотрим варианты у моря.",
                "ask_field": "ready",
                "constraint_patch": {
                    "search_area": "Новый Свет",
                    "preferred_localities": ["Новый Свет"],
                    "flexible_start": True,
                },
            }
        ),
        confirmed_fields=[],
    )
    assert turn is not None
    assert turn.constraint_patch["preferred_localities"] == ["Новый Свет"]
    assert "interests" not in turn.constraint_patch


def test_context_preserves_rag_after_large_place_data_and_is_valid_json() -> None:
    context = {
        "place_candidates": [{"name": "Большая карточка", "body": "x" * 4000}] * 8,
        "catalog_routes": [{"route_id": "verified-id", "title": "Форос"}],
        "knowledge": [
            {"title": "Симеиз", "body": "Берег и парк. " * 200, "source": "reviewed-article"}
        ],
        "user_preferences_prior": {"interests": ["природа"]},
    }
    note = planning_state_note(
        {"city": "Симферополь", "search_area": SOUTH_COAST},
        ["search_area"],
        None,
        context,
    )
    data = json.loads(note.split("backend_DATA: ", 1)[1])
    assert data["knowledge"][0]["source"] == "reviewed-article"
    assert data["catalog_routes"][0]["route_id"] == "verified-id"
    assert data["user_preferences_prior"]["interests"] == ["природа"]
    assert "Симферополь" not in note
    assert len(json.dumps(data, ensure_ascii=False)) <= 18000
    assert len(context["place_candidates"]) == 8  # original context is not mutated


@pytest.mark.parametrize("semantic_only", [False, True])
async def test_real_orchestration_feeds_catalogue_and_rag_to_provider(monkeypatch, semantic_only):
    session = MagicMock(spec=AsyncSession)
    session.scalars = AsyncMock(
        return_value=SimpleNamespace(
            all=lambda: [
                SimpleNamespace(
                    role="user",
                    intent="constraints",
                    text=("Тишины, воды и красивых видов бы" if semantic_only else REQUEST),
                ),
            ]
        )
    )
    user = SimpleNamespace(
        id=uuid4(),
        preferred_categories=["nature"],
        preferred_difficulty=None,
        travels_with_kids=False,
        travels_with_pets=False,
    )
    match = AsyncMock(
        return_value=SimpleNamespace(
            ideal=[SimpleNamespace(route=SimpleNamespace(id=uuid4(), name="Форос и парк"))],
            close=[],
        )
    )
    monkeypatch.setattr(service.match_service, "match_routes", match)
    monkeypatch.setattr(service, "prefetch_context", AsyncMock(return_value={}))
    settings = Settings(ai_planning_enabled=True, rag_enabled=True)
    monkeypatch.setattr(service, "effective_ai_provider_settings", AsyncMock(return_value=settings))
    provider = SimpleNamespace(
        chat_turn=AsyncMock(
            return_value=ChatTurnResult(
                assistant_text="Город старта?",
                ask_field="city",
                structured_parse="ok" if semantic_only else "fallback",
                goal="discover" if semantic_only else None,
                provider="deepseek",
            )
        )
    )
    monkeypatch.setattr(service, "get_ai_planning_provider", lambda _: provider)
    retriever = SimpleNamespace(
        retrieve=AsyncMock(
            return_value=SimpleNamespace(
                chunks=[
                    SimpleNamespace(
                        title="Форос", body="Парк у моря", source="verified", content_type="article"
                    ),
                ]
            )
        )
    )
    monkeypatch.setattr(service, "TourismKnowledgeRetriever", lambda **_: retriever)
    monkeypatch.setattr(service, "build_embedder", lambda _: None)
    result = await service._assistant_from_ai(
        session,
        planning=SimpleNamespace(id=uuid4()),
        user=user,
        settings=settings,
        constraints={"city": "Симферополь", **({} if semantic_only else discovery_patch(REQUEST))},
        confirmed_fields=[] if semantic_only else ["search_area", "preferred_localities"],
    )
    context = provider.chat_turn.call_args.kwargs["tool_context"]
    assert context["catalog_routes"][0]["title"] == "Форос и парк"
    assert context["knowledge"][0]["source"] == "verified"
    assert context["user_preferences_prior"]["interests"] == ["nature"]
    request = retriever.retrieve.call_args.kwargs["request"]
    assert request.locality is None
    assert "Симферополь" not in request.query
    if not semantic_only:
        assert "Форос" in request.query
    assert result[2] is (not semantic_only)
    assert provider.chat_turn.await_count == (2 if semantic_only else 1)


@pytest.mark.parametrize("provider_name", ["deepseek", "gemini", "lmstudio"])
async def test_all_provider_requests_preserve_knowledge_and_tool_results(provider_name):
    seen = []
    answer = json.dumps(
        {
            "assistant_text": "Можно сравнить маршруты у моря.",
            "ask_field": "ready",
            "goal": "compare",
            "clarification_reason": "Проверка контракта",
        }
    )

    def handler(request):
        body = json.loads(request.content)
        if provider_name == "gemini":
            note = body["systemInstruction"]["parts"][0]["text"]
            response = {"candidates": [{"content": {"parts": [{"text": answer}]}}]}
        else:
            note = body["messages"][1]["content"]
            assert body["max_tokens"] == 1024
            response = {"choices": [{"message": {"content": answer}}]}
        data = json.loads(note.split("backend_DATA: ", 1)[1])
        assert data["knowledge"][0]["source"] == "source-marker"
        assert data["tool_results"][0]["data"]["id"] == "actual-db-place"
        assert data["catalog_routes"][0]["route_id"] == "actual-db-route"
        seen.append(data)
        return httpx.Response(200, json=response)

    common = {
        "api_key": "fixture",
        "model": "fixture",
        "timeout_seconds": 5,
        "transport": httpx.MockTransport(handler),
    }
    if provider_name == "deepseek":
        provider = DeepSeekProvider(**common)
    elif provider_name == "gemini":
        provider = GeminiProvider(**common)
    else:
        provider = LMStudioProvider(base_url="https://model.test/v1", **common)
    result = await provider.chat_turn(
        messages=[ChatMessage(role="user", content=REQUEST)],
        constraints={"city": "Крым", "search_area": SOUTH_COAST},
        confirmed_fields=["search_area"],
        tool_context={
            "place_candidates": [{"body": "x" * 3000}] * 6,
            "knowledge": [{"body": "Парк. " * 300, "source": "source-marker"}],
            "catalog_routes": [{"route_id": "actual-db-route"}],
            "tool_results": [
                {"name": "get_place_details", "ok": True, "data": {"id": "actual-db-place"}}
            ],
        },
    )
    assert len(seen) == 1
    assert result.structured_parse == "ok"
    assert result.goal == "compare"
    assert result.clarification_reason == "Проверка контракта"


@pytest.mark.parametrize(
    "goal", ["discover", "compare", "place_info", "custom", "clarify", "drop_database", [], None]
)
def test_goal_is_allowlisted(goal):
    turn = parse_structured_turn(json.dumps({"assistant_text": "Ответ", "goal": goal}))
    assert turn is not None
    assert turn.goal == (goal if isinstance(goal, str) and goal != "drop_database" else None)
    assert turn.ask_field == "ready"


async def test_comparison_rechecks_previous_card_ids_not_new_matches(monkeypatch):
    route_id = uuid4()
    old_id = uuid4()
    reader = AsyncMock(return_value=[])
    monkeypatch.setattr(service.match_service, "public_catalogue_routes", reader)
    rows = [
        SimpleNamespace(
            role="assistant",
            payload={"blocks": [{"type": "catalog_match", "routes": [{"route_id": str(old_id)}]}]},
        ),
        SimpleNamespace(
            role="assistant",
            payload={
                "blocks": [{"type": "catalog_match", "routes": [{"route_id": str(route_id)}]}]
            },
        ),
    ]
    result = await service._comparison_context(MagicMock(spec=AsyncSession), uuid4(), rows=rows)
    assert reader.call_args.args[1] == [route_id]
    assert result == {"comparison_routes": []}  # withdrawn route is not copied from history


@pytest.mark.parametrize("follow_status", ["ok", "fallback"])
async def test_tool_round_preserves_extraction_and_exposes_results(monkeypatch, follow_status):
    first = ChatTurnResult(
        assistant_text="Посмотрим Симеиз.",
        ask_field="ready",
        proposed_constraints={"search_area": SOUTH_COAST},
        tool_requests=({"name": "get_place_details", "arguments": {"place_id": str(uuid4())}},),
    )
    provider = SimpleNamespace(
        chat_turn=AsyncMock(
            return_value=ChatTurnResult(
                assistant_text="По данным справочника здесь есть парк.",
                ask_field="ready",
                structured_parse=follow_status,
            )
        )
    )
    monkeypatch.setattr(
        service,
        "execute_tool",
        AsyncMock(
            return_value=SimpleNamespace(
                name="get_place_details",
                ok=True,
                data={"name": "Парк", "source": "actual-place"},
            )
        ),
    )
    result, context, _ = await service._run_tool_rounds(
        MagicMock(spec=AsyncSession),
        provider=provider,
        result=first,
        constraints={"city": "Крым"},
        confirmed_fields=[],
        chat_messages=[],
        place_hints=[],
        tool_context={},
    )
    assert result.proposed_constraints == {"search_area": SOUTH_COAST}
    assert provider.chat_turn.call_args.kwargs["tool_context"]["tool_results"][0]["ok"] is True
    assert context["tool_results"][0]["data"]["source"] == "actual-place"
    if follow_status == "fallback":
        assert result is first

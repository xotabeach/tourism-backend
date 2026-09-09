"""Behavioural regressions for the real post-message orchestration."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.config import Settings
from tourism_backend.modules.route_builder.application import session_service as service
from tourism_backend.modules.route_builder.application.ai import ChatMessage, ChatTurnResult
from tourism_backend.modules.route_builder.application.schemas import RoutePlanningMessageIn
from tourism_backend.modules.route_builder.application.structured_turn import parse_structured_turn
from tourism_backend.modules.route_builder.infrastructure.models import RoutePlanningSession


@pytest.fixture
def chat(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    user = SimpleNamespace(
        id=uuid4(),
        preferred_categories=[],
        preferred_difficulty=None,
        travels_with_kids=False,
        travels_with_pets=False,
    )
    now = datetime.now(UTC)
    planning = RoutePlanningSession(
        id=uuid4(),
        user_id=user.id,
        status="active",
        constraints={
            "city": "Ялта",
            "transport_mode": "walk",
            "duration": "d1_2",
            "interests": ["море"],
        },
        confirmed_fields=["city", "transport_mode", "duration", "interests", "people"],
        created_at=now,
        updated_at=now,
    )
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=user)
    ai = AsyncMock(
        return_value=(
            ChatTurnResult(
                assistant_text="Показать готовые варианты?",
                ask_field="ready",
                quick_replies=({"id": "want_generate", "label": "Да, конечно"},),
            ),
            "test",
            False,
            {},
            [],
        )
    )
    match = AsyncMock(return_value=SimpleNamespace(ideal=[], close=[]))
    generate = AsyncMock()
    monkeypatch.setattr(service, "refresh_user_travel_plus", AsyncMock())
    monkeypatch.setattr(service, "require_ai_chat", lambda _: None)
    monkeypatch.setattr(service, "_owned_session", AsyncMock(return_value=planning))
    monkeypatch.setattr(service, "_message_count", AsyncMock(return_value=2))
    monkeypatch.setattr(service, "_assistant_from_ai", ai)
    monkeypatch.setattr(service.match_service, "match_routes", match)
    monkeypatch.setattr(service.generate_service, "generate_route", generate)
    return SimpleNamespace(
        user=user, planning=planning, session=session, ai=ai, match=match, generate=generate
    )


async def _post(chat: SimpleNamespace, **payload: object):
    return await service.post_message(
        chat.session,
        user_id=chat.user.id,
        session_id=chat.planning.id,
        payload=RoutePlanningMessageIn.model_validate(payload),
        settings=Settings(),
    )


async def test_controls_are_one_model_turn_and_do_not_reprint_form(chat: SimpleNamespace) -> None:
    result = await _post(
        chat,
        text="Бюджет 5500 ₽, без питомцев, избегать толп",
        controls={
            "budget_amount": 5500,
            "with_children": True,
            "with_pets": False,
            "avoid_crowds": True,
        },
    )
    chat.ai.assert_awaited_once()
    passed = chat.ai.call_args.kwargs["constraints"]
    assert passed["budget_amount"] == 5500
    assert passed["with_children"] is True
    assert passed["with_pets"] is False
    assert passed["avoid_crowds"] is True
    assert len(chat.session.add.call_args_list) == 2  # one user and one assistant row
    assert all(block.type not in {"toggle", "slider"} for block in result.blocks)
    chat.match.assert_awaited_once()
    assert result.ask_field == "ready"


async def test_empty_catalog_never_generates_without_choice(chat: SimpleNamespace) -> None:
    result = await _post(chat, text="Показать варианты", action_id="want_generate")
    chat.match.assert_awaited_once()
    chat.generate.assert_not_awaited()
    assert result.proposal is None
    assert "нет подходящих" in result.text
    assert any(
        action["id"] == "build_custom_route" for block in result.blocks for action in block.actions
    )


async def test_early_search_does_not_require_transport_and_duration(chat: SimpleNamespace) -> None:
    chat.planning.confirmed_fields = ["city", "interests"]
    await _post(chat, text="Подбери маршрут", want_generate=True)
    chat.ai.assert_not_awaited()
    chat.match.assert_awaited_once()
    chat.generate.assert_not_awaited()


async def test_screenshot_requests_show_catalogue_without_mandatory_start(chat: SimpleNamespace):
    chat.planning.confirmed_fields = []
    chat.planning.constraints = {"city": "Симферополь"}
    route_id = uuid4()
    chat.match.return_value = SimpleNamespace(
        ideal=[SimpleNamespace(route=SimpleNamespace(id=route_id, name="Симеиз и парк"))], close=[]
    )
    chat.ai.return_value = (
        ChatTurnResult(
            assistant_text="Принял. Город старта?", ask_field="city", structured_parse="fallback"
        ),
        "deepseek",
        True,
        {},
        [],
    )
    first = await _post(
        chat,
        text=(
            "привет! сможешь мне подобрать маршруты вдоль южного берега? "
            "мне очень нравятся поселки по типу Фороса, Симеиза"
        ),
    )
    assert first.ask_field == "ready"
    assert "Принял" not in first.text
    assert "city" not in first.confirmed_fields
    assert first.blocks[0].type == "catalog_match"
    assert first.blocks[0].routes[0].route_id == str(route_id)
    assert chat.match.call_args.kwargs["params"].search_area == "Южный берег Крыма"
    second = await _post(
        chat,
        text=(
            "какой город старта? не нужен мне он, просто хочу начать откуда то "
            "на южном берегу Крыма. ты же гид, предлагай"
        ),
    )
    assert second.ask_field == "ready"
    assert second.blocks[0].type == "catalog_match"
    assert chat.planning.constraints["flexible_start"] is True
    assert chat.planning.constraints["preferred_localities"] == ["Форос", "Симеиз"]
    chat.generate.assert_not_awaited()


async def test_model_extracted_area_is_searched_before_claiming_no_matches(chat: SimpleNamespace):
    chat.planning.confirmed_fields = []
    chat.ai.return_value = (
        ChatTurnResult(
            assistant_text="Посмотрим восточное побережье.",
            ask_field="ready",
            proposed_constraints={"search_area": "Новый Свет"},
        ),
        "gemini",
        False,
        {},
        [],
    )
    result = await _post(chat, text="Хочу туда, где снимали любимое кино, предложишь варианты?")
    chat.match.assert_awaited_once()
    assert chat.match.call_args.kwargs["params"].search_area == "Новый Свет"
    assert result.ask_field == "ready"
    assert "не нашлось" in result.text
    assert result.proposal is None
    chat.generate.assert_not_awaited()


async def test_discovery_does_not_allow_custom_build_without_parameters(chat: SimpleNamespace):
    chat.planning.constraints["search_area"] = "Южный берег Крыма"
    chat.planning.confirmed_fields = ["search_area"]
    result = await _post(chat, text="Собрать свой", action_id="build_custom_route")
    assert chat.planning.constraints["planning_mode"] == "custom"
    assert result.ask_field == "city"
    chat.generate.assert_not_awaited()


async def test_natural_quick_reply_reaches_model(chat: SimpleNamespace) -> None:
    await _post(chat, text="Да, конечно", action_id="reply")
    chat.ai.assert_awaited_once()
    chat.generate.assert_not_awaited()


async def test_factual_reply_is_not_replaced_with_start_question(chat: SimpleNamespace):
    chat.planning.confirmed_fields = []
    chat.ai.return_value = (
        ChatTurnResult(
            assistant_text="По справочнику, здесь есть прогулочный парк.", ask_field="ready"
        ),
        "gemini",
        False,
        {},
        [],
    )
    result = await _post(chat, text="Расскажи об этом парке, пожалуйста")
    assert result.text == "По справочнику, здесь есть прогулочный парк."
    assert result.ask_field == "ready"
    chat.match.assert_not_awaited()


@pytest.mark.parametrize(
    "query",
    [
        "Хочу спокойную прогулку у моря, предложи сам",
        "Посоветуй что-нибудь для поездки с детьми",
        "Тишины, воды и красивых видов бы — доверяюсь тебе",
    ],
)
async def test_semantic_discovery_needs_no_destination(chat: SimpleNamespace, query: str):
    chat.planning.confirmed_fields = []
    chat.planning.constraints = {"city": "Симферополь", "duration": "d7plus"}
    chat.ai.return_value = (
        ChatTurnResult(
            assistant_text="Выберите город",
            ask_field="city",
            goal="discover",
            proposed_constraints={"interests": ["море"], "pace": "calm"},
        ),
        "gemini",
        False,
        {},
        [],
    )
    chat.match.return_value = SimpleNamespace(
        ideal=[SimpleNamespace(route=SimpleNamespace(id=uuid4(), name="Море и парк"))], close=[]
    )
    result = await _post(chat, text=query)
    params = chat.match.call_args.kwargs["params"]
    assert params.search_area == "Крым"
    assert params.interests == ["море"]
    assert result.ask_field == "ready"
    assert result.blocks[0].type == "catalog_match"
    assert "city" not in result.confirmed_fields
    assert "search_area" not in result.confirmed_fields  # app scope, not guessed preference
    assert chat.planning.constraints["dialogue_goal"] == "discover"
    chat.generate.assert_not_awaited()


async def test_factual_question_interrupts_custom_questionnaire(chat: SimpleNamespace):
    chat.planning.constraints["planning_mode"] = "custom"
    chat.planning.confirmed_fields = []
    chat.ai.return_value = (
        ChatTurnResult(
            assistant_text="Вот описание из справочника.", ask_field="ready", goal="place_info"
        ),
        "test",
        False,
        {},
        [],
    )
    result = await _post(chat, text="Расскажи об истории этого места")
    assert result.text == "Вот описание из справочника."
    assert result.ask_field == "ready"
    chat.match.assert_not_awaited()
    chat.generate.assert_not_awaited()


async def test_compare_keeps_the_shown_options_and_does_not_search(chat: SimpleNamespace):
    chat.ai.return_value = (
        ChatTurnResult(
            assistant_text="Первый короче, второй — для долгой прогулки.",
            goal="compare",
            ask_field="ready",
        ),
        "test",
        False,
        {"comparison_routes": [{"route_id": "shown", "title": "Прогулка"}]},
        [],
    )
    result = await _post(chat, text="Сравни эти варианты")
    assert result.text.startswith("Первый короче")
    assert not any(block.type == "catalog_match" for block in result.blocks)
    chat.match.assert_not_awaited()


async def test_custom_goal_does_not_authorize_generation(chat: SimpleNamespace):
    chat.ai.return_value = (
        ChatTurnResult(assistant_text="Обсудим собственный план", goal="custom", ask_field="ready"),
        "test",
        False,
        {},
        [],
    )
    await _post(chat, text="Хочу подробный собственный план")
    chat.generate.assert_not_awaited()


async def test_yes_in_place_question_does_not_trigger_catalogue(chat: SimpleNamespace):
    chat.planning.constraints["dialogue_goal"] = "place_info"
    chat.planning.constraints["search_area"] = "Крым"
    chat.planning.confirmed_fields.append("search_area")
    chat.ai.return_value = (
        ChatTurnResult(
            assistant_text="Продолжим рассказ о месте.", goal="place_info", ask_field="ready"
        ),
        "test",
        False,
        {},
        [],
    )
    result = await _post(chat, text="Да")
    assert result.text == "Продолжим рассказ о месте."
    chat.ai.assert_awaited_once()
    chat.match.assert_not_awaited()


async def test_later_spoken_correction_can_change_a_confirmed_choice(chat: SimpleNamespace) -> None:
    chat.ai.return_value = (
        ChatTurnResult(
            assistant_text="Тогда посмотрим автомобильные варианты.",
            ask_field="ready",
            proposed_constraints={"transport_mode": "car"},
        ),
        "test",
        False,
        {},
        [],
    )
    result = await _post(chat, text="Передумал, теперь поедем на машине")
    assert chat.planning.constraints["transport_mode"] == "car"
    assert result.proposed_constraints == {"transport_mode": "car"}


async def test_model_cannot_override_controls_submitted_in_same_turn(chat: SimpleNamespace) -> None:
    chat.ai.return_value = (
        ChatTurnResult(
            assistant_text="Показать варианты?",
            ask_field="ready",
            proposed_constraints={"budget_amount": 100, "with_pets": True},
        ),
        "test",
        False,
        {},
        [],
    )
    result = await _post(
        chat, text="Подтверждаю", controls={"budget_amount": 5500, "with_pets": False}
    )
    assert chat.planning.constraints["budget_amount"] == 5500
    assert chat.planning.constraints["with_pets"] is False
    assert result.proposed_constraints == {}


@pytest.mark.parametrize(
    "controls",
    [
        {"budget_amount": True},
        {"budget_amount": -1},
        {"with_children": "false"},
        {"password": "x"},
    ],
)
def test_invalid_controls_rejected(controls: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RoutePlanningMessageIn(text="Подтверждаю", controls=controls)


def test_quick_replies_are_bounded_and_allowlisted() -> None:
    turn = parse_structured_turn(
        json.dumps(
            {
                "assistant_text": "Продолжим?",
                "quick_replies": [
                    {"id": "reply", "label": "Вперёд!"},
                    {"id": "delete_account", "label": "Да"},
                    {"id": "reply", "label": "<script>"},
                    {"id": "reply", "label": "a" * 61},
                ],
            }
        )
    )
    assert turn is not None
    assert turn.quick_replies == ({"id": "reply", "label": "Вперёд!"},)


def test_actual_question_is_present_in_retrieval_query() -> None:
    query = service._retrieval_query(
        [ChatMessage(role="user", content="Где отдохнуть с детьми?")],
        {"city": "Ялта", "interests": ["море"]},
    )
    assert "Где отдохнуть с детьми?" in query
    assert "Ялта" in query


def test_profile_only_fills_fields_not_explicitly_selected(chat: SimpleNamespace) -> None:
    chat.user.preferred_categories = ["горы"]
    chat.user.travels_with_pets = True
    params = service._constraints_with_preferences(
        {"interests": ["море"], "with_pets": False}, ["interests", "with_pets"], chat.user
    )
    assert params == {"interests": ["море"], "with_pets": False}

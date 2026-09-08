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
    assert result.blocks[0].actions == [{"id": "want_generate", "label": "Да, конечно"}]


async def test_empty_catalog_never_generates_without_choice(chat: SimpleNamespace) -> None:
    result = await _post(chat, text="Показать варианты", action_id="want_generate")
    chat.match.assert_awaited_once()
    chat.generate.assert_not_awaited()
    assert result.proposal is None
    assert "нет подходящих" in result.text
    assert any(
        action["id"] == "build_custom_route" for block in result.blocks for action in block.actions
    )


async def test_early_search_asks_missing_transport_and_duration(chat: SimpleNamespace) -> None:
    chat.planning.confirmed_fields = ["city", "interests"]
    await _post(chat, text="Подбери маршрут", want_generate=True)
    chat.ai.assert_awaited_once()
    chat.match.assert_not_awaited()
    chat.generate.assert_not_awaited()


async def test_natural_quick_reply_reaches_model(chat: SimpleNamespace) -> None:
    await _post(chat, text="Да, конечно", action_id="reply")
    chat.ai.assert_awaited_once()
    chat.generate.assert_not_awaited()


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

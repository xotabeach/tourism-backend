"""Unit tests for mock AI planning chat_turn."""

import pytest

from tourism_backend.modules.route_builder.application.ai import ChatMessage
from tourism_backend.modules.route_builder.infrastructure.ai_mock import (
    MockAIPlanningProvider,
)


@pytest.mark.asyncio
async def test_mock_chat_turn_asks_city_when_unconfirmed() -> None:
    provider = MockAIPlanningProvider()
    result = await provider.chat_turn(
        messages=[ChatMessage(role="user", content="Люблю дворцы")],
        constraints={"city": "Ялта"},
        confirmed_fields=[],
    )
    assert result.provider == "mock"
    assert result.ask_field == "city"
    # Draft city must not be stated as already chosen.
    assert "вокруг Ялта" not in result.assistant_text
    assert "параметров достаточно" not in result.assistant_text.casefold()


@pytest.mark.asyncio
async def test_mock_chat_turn_transport_chips_when_city_confirmed() -> None:
    provider = MockAIPlanningProvider()
    result = await provider.chat_turn(
        messages=[ChatMessage(role="user", content="Больше гор")],
        constraints={"city": "Ялта", "interests": ["горы"]},
        confirmed_fields=["city", "interests", "pace", "duration"],
    )
    assert result.ask_field == "transport_mode"
    assert "transport_car" in result.action_ids
    assert "гор" in result.assistant_text.casefold() or "машин" in result.assistant_text.casefold()

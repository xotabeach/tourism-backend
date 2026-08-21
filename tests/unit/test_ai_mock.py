"""Unit tests for mock AI planning chat_turn."""

import pytest

from tourism_backend.modules.route_builder.application.ai import ChatMessage
from tourism_backend.modules.route_builder.infrastructure.ai_mock import (
    MockAIPlanningProvider,
)


@pytest.mark.asyncio
async def test_mock_chat_turn_mentions_city() -> None:
    provider = MockAIPlanningProvider()
    result = await provider.chat_turn(
        messages=[ChatMessage(role="user", content="Люблю дворцы")],
        constraints={"city": "Ялта"},
    )
    assert result.provider == "mock"
    assert "Ялта" in result.assistant_text
    assert "дворцы" in result.assistant_text

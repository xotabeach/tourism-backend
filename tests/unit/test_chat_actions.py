"""Unit tests for chat clarification action chips."""

from tourism_backend.modules.route_builder.application.chat_actions import (
    clarification_action_blocks,
)


def test_clarification_actions_include_generate() -> None:
    blocks = clarification_action_blocks({"city": "Ялта"})
    assert len(blocks) == 1
    ids = {item["id"] for item in blocks[0].actions}
    assert "want_generate" in ids
    assert "say_mood_calm" in ids

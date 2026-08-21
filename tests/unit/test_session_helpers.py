"""Unit tests for planning session helpers."""

from datetime import UTC, datetime
from uuid import uuid4

from tourism_backend.modules.route_builder.application.session_service import (
    _parse_blocks,
    _session_out,
    _stored_message_out,
)
from tourism_backend.modules.route_builder.infrastructure.models import (
    RoutePlanningMessage,
    RoutePlanningSession,
)


def test_parse_blocks_allowlist() -> None:
    blocks = _parse_blocks(
        [
            {
                "type": "place_chip",
                "place_id": str(uuid4()),
                "title": "Ласточкино гнездо",
            },
            {"type": "actions", "actions": [{"id": "want_generate", "label": "Подбери"}]},
            {"type": "unknown_html", "html": "<script>"},
            "not-a-dict",
            {"type": "place_chip"},  # invalid
        ]
    )
    assert len(blocks) == 2
    assert blocks[0].type == "place_chip"
    assert blocks[1].type == "actions"


def test_session_and_message_out_helpers() -> None:
    now = datetime.now(UTC)
    session = RoutePlanningSession(
        id=uuid4(),
        user_id=uuid4(),
        status="active",
        constraints={"city": "Ялта", "duration": "d3_5", "people": 2, "interests": []},
        created_at=now,
        updated_at=now,
    )
    out = _session_out(session, ai_planning_enabled=True)
    assert out.session_id == str(session.id)
    assert out.ai_planning_enabled is True
    assert out.created_at == now

    message = RoutePlanningMessage(
        id=uuid4(),
        session_id=session.id,
        user_id=session.user_id,
        role="assistant",
        text="ok",
        intent="on_topic_travel",
        proposal_id=None,
        payload={
            "blocks": [
                {
                    "type": "actions",
                    "actions": [{"id": "want_generate", "label": "Подбери маршрут"}],
                }
            ]
        },
        created_at=now,
        updated_at=now,
    )
    stored = _stored_message_out(message)
    assert stored.message_id == str(message.id)
    assert len(stored.blocks) == 1
    assert stored.blocks[0].type == "actions"

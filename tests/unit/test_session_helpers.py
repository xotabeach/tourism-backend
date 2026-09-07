"""Unit tests for planning session helpers."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from tourism_backend.modules.knowledge.infrastructure.models import KnowledgeChunk
from tourism_backend.modules.route_builder.application.ai import AIProviderBusyError
from tourism_backend.modules.route_builder.application.session_service import (
    _catalog_match_block,
    _compose_assistant_blocks,
    _parse_blocks,
    _persisted_preferences_prior,
    _provider_error_turn,
    _session_out,
    _stored_message_out,
    llm_history_stmt,
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
        confirmed_fields=["city"],
        created_at=now,
        updated_at=now,
    )
    out = _session_out(session, ai_planning_enabled=True)
    assert out.session_id == str(session.id)
    assert out.ai_planning_enabled is True
    assert out.confirmed_fields == ["city"]
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


def test_compose_assistant_blocks_recommendations_only_when_requested() -> None:
    tip = {
        "id": "yubk",
        "title": "ЮБК летом",
        "body": "Тёплые пляжи и набережные.",
        "accept_action": "rec_yubk",
    }
    without = _compose_assistant_blocks(
        constraints={"city": "Крым"},
        confirmed_fields=[],
        ask_field=None,
        action_ids=["want_generate"],
        tool_context={"seasonal_recommendations": [tip]},
        include_recommendations=False,
    )
    assert all(b.type != "recommendation_card" for b in without)

    with_recs = _compose_assistant_blocks(
        constraints={"city": "Крым"},
        confirmed_fields=[],
        ask_field=None,
        action_ids=["want_generate"],
        tool_context={"seasonal_recommendations": [tip]},
        include_recommendations=True,
    )
    assert any(b.type == "recommendation_card" for b in with_recs)


def test_catalog_match_block_from_bands() -> None:
    assert _catalog_match_block(SimpleNamespace(ideal=[], close=[], related=[])) is None

    route_id = uuid4()
    route = SimpleNamespace(
        id=route_id,
        name="Ялта на день",
        cover_image_url=None,
        distance_meters=4200,
        transport_mode="car",
        suitable_for_children=True,
        seasonality=["лето"],
        difficulty="easy",
        estimated_duration_minutes=240,
        stops_count=4,
    )
    matched = SimpleNamespace(
        ideal=[SimpleNamespace(route=route, score=0.9, reasons=["рядом"])],
        close=[],
        related=[],
    )
    block = _catalog_match_block(matched)
    assert block is not None
    assert block.type == "catalog_match"
    assert block.routes[0].route_id == str(route_id)
    assert block.routes[0].title == "Ялта на день"
    assert block.routes[0].locality_label is None

    with_city = _catalog_match_block(matched, locality_label="Ялта")
    assert with_city is not None
    assert with_city.routes[0].locality_label == "Ялта"


def test_knowledge_chunk_model_columns() -> None:
    assert KnowledgeChunk.__tablename__ == "knowledge_chunks"
    assert "doc_id" in KnowledgeChunk.__table__.c
    assert "body" in KnowledgeChunk.__table__.c


def test_control_patch_helpers() -> None:
    from tourism_backend.modules.route_builder.application.session_service import (
        _control_patch,
    )

    assert _control_patch("budget_amount", 1500.7) == {"budget_amount": 1500}
    assert _control_patch("budget_amount", -5) == {"budget_amount": 0}
    assert _control_patch("with_children", True) == {"with_children": True}
    assert _control_patch("with_pets", False) == {"with_pets": False}
    assert _control_patch("with_children", None) == {"with_children": True}
    assert _control_patch("with_pets", None) == {"with_pets": True}
    assert _control_patch("unknown", 1) is None


def test_provider_error_turn_distinguishes_busy_from_outage() -> None:
    busy = _provider_error_turn(AIProviderBusyError("lm_studio_busy"), ["city"])
    outage = _provider_error_turn(RuntimeError("connection reset"), ["city"])
    assert "занят" in busy.assistant_text.casefold()
    assert "подождите" in busy.assistant_text.casefold()
    assert "не удалось связаться" in outage.assistant_text.casefold()
    assert busy.assistant_text != outage.assistant_text


def test_llm_history_stmt_trims_in_sql_and_omits_flagged_user_turns() -> None:
    from sqlalchemy.dialects import postgresql

    compiled = llm_history_stmt(uuid4(), limit=12).compile(dialect=postgresql.dialect())
    sql = str(compiled).lower()
    bound = " ".join(str(value) for value in compiled.params.values()).lower()
    assert "limit" in sql
    assert "desc" in sql
    assert "redacted" in bound
    assert "crisis" in bound
    assert "injection_attempt" in bound


def test_empty_profile_yields_no_prior() -> None:
    user = SimpleNamespace(
        preferred_categories=None,
        preferred_difficulty=None,
        travels_with_kids=False,
        travels_with_pets=False,
    )
    assert _persisted_preferences_prior(user) == {}


def test_populated_profile_yields_only_the_set_fields() -> None:
    user = SimpleNamespace(
        preferred_categories=["горы", "море"],
        preferred_difficulty="hard",
        travels_with_kids=False,
        travels_with_pets=True,
    )
    prior = _persisted_preferences_prior(user)
    assert prior == {
        "interests": ["горы", "море"],
        "pace_hint": "hard",
        "with_pets": True,
    }
    assert "with_children" not in prior


async def test_tool_round_dropped_when_the_turn_is_out_of_budget() -> None:
    """A slow tool round must not push the turn past the client's patience.

    Measured on production: the first model call takes 3.4-13.8s and the tool
    round another 3.4-13.8s, so without a shared budget one turn could spend
    twice the per-call timeout while the app had already given up.
    """
    import asyncio

    from tourism_backend.modules.route_builder.application.ai import ChatTurnResult
    from tourism_backend.modules.route_builder.application.session_service import (
        _run_tool_rounds,
    )

    first = ChatTurnResult(
        assistant_text="Первый ответ",
        proposed_constraints=None,
        ask_field=None,
        action_ids=(),
        tool_requests=({"name": "search_places", "arguments": {"query": "Симеиз"}},),
        provider="gemini",
        structured_parse="ok",
    )

    class _SlowProvider:
        async def chat_turn(self, **_: object) -> ChatTurnResult:
            await asyncio.sleep(5)
            raise AssertionError("the follow-up must be abandoned, not awaited")

    result, _, _ = await _run_tool_rounds(
        SimpleNamespace(),  # type: ignore[arg-type]
        provider=_SlowProvider(),
        result=first,
        constraints={"city": "Крым"},
        confirmed_fields=[],
        chat_messages=[],
        place_hints=[],
        tool_context={},
        budget_seconds=0.05,
    )

    assert result.assistant_text == "Первый ответ"


def test_notification_schema_accepts_every_kind_the_database_allows() -> None:
    """A kind the DB writes but the schema rejects 500s the whole inbox.

    Seen in production: `article_published` reached the list endpoint and
    every notifications request for that user failed validation.
    """
    import re
    from typing import get_args

    from sqlalchemy import CheckConstraint

    from tourism_backend.modules.notifications.application.schemas import (
        NotificationKind,
        NotificationTargetType,
    )
    from tourism_backend.modules.notifications.infrastructure.models import Notification

    constraints = {
        c.name: str(c.sqltext)
        for c in Notification.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name
    }
    checks = (
        ("ck_notifications_kind", get_args(NotificationKind)),
        ("ck_notifications_target_type", get_args(NotificationTargetType)),
    )
    for name, allowed in checks:
        in_db = set(re.findall(r"'([a-z_]+)'", constraints[name]))
        missing = in_db - set(allowed)
        assert not missing, f"{name}: the DB allows values the API cannot return: {missing}"


async def test_opening_a_chat_warms_the_embedder_without_failing_the_request() -> None:
    """Entering the chat must start the ~9.5s model load, and survive it failing.

    The load is what made the first message after a restart time out on the
    client; doing it here buys it the time the user spends typing.
    """
    from tourism_backend.config import Settings
    from tourism_backend.modules.route_builder.application import (
        session_service as service_module,
    )
    from tourism_backend.modules.route_builder.application.session_service import (
        warm_rag_embedder,
    )

    warmed: list[str] = []

    class _Embedder:
        model_id = "test-model"

        async def warm(self) -> None:
            warmed.append(self.model_id)

        async def embed(self, text: str) -> list[float]:
            raise AssertionError("warmup must not run an actual embedding")

    settings = Settings(rag_enabled=True)
    with mock.patch.object(service_module, "build_embedder", return_value=_Embedder()):
        await warm_rag_embedder(settings)
    assert warmed == ["test-model"]

    # RAG off: nothing to load, and nothing should be attempted.
    warmed.clear()
    with mock.patch.object(service_module, "build_embedder", return_value=_Embedder()):
        await warm_rag_embedder(Settings(rag_enabled=False))
    assert warmed == []

    class _Broken:
        async def warm(self) -> None:
            raise RuntimeError("no model on disk")

    with mock.patch.object(service_module, "build_embedder", return_value=_Broken()):
        await warm_rag_embedder(settings)  # must not raise

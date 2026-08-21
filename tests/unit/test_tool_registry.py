"""Unit tests for planning ToolRegistry."""

import pytest

from tourism_backend.modules.route_builder.application.chat_actions import (
    interactive_control_blocks,
    prefer_ready_ask_field,
)
from tourism_backend.modules.route_builder.application.session_service import (
    _compose_assistant_blocks,
    _control_patch,
    _try_parse_block,
)
from tourism_backend.modules.route_builder.application.tool_registry import (
    _seasonal_recommendations,
    parse_tool_calls,
    recommendation_accept_patch,
    season_from_month,
)


def test_parse_tool_calls_allowlist() -> None:
    calls = parse_tool_calls(
        [
            {"name": "search_places", "arguments": {"city": "Ялта", "limit": 4}},
            {"name": "drop_table", "arguments": {}},
            {"name": "seasonal_recommendations", "args": {"season": "summer"}},
        ]
    )
    assert len(calls) == 2
    assert calls[0].name == "search_places"
    assert calls[1].name == "seasonal_recommendations"
    assert parse_tool_calls("nope") == []
    assert parse_tool_calls([{"name": "search_places", "arguments": "bad"}])[0].arguments == {}
    assert (
        parse_tool_calls(
            [
                {
                    "name": "search_places",
                    "arguments": {"city": "x" * 100, "bad": {"nested": 1}},
                }
            ]
        )[0]
        .arguments["city"]
        .endswith("x")
    )


def test_seasonal_recommendations_summer() -> None:
    payload = _seasonal_recommendations({"season": "summer"}, {})
    assert payload["season"] == "summer"
    assert payload["items"]
    bodies = " ".join(item.get("body", "") for item in payload["items"]).casefold()
    assert "море" in bodies or "пляж" in bodies


def test_recommendation_accept_patch() -> None:
    patch = recommendation_accept_patch("accept_rec_summer_foros")
    assert patch is not None
    assert patch["city"] == "Ялта"
    assert "море" in (patch.get("interests_add") or [])


def test_prefer_ready_and_controls() -> None:
    assert prefer_ready_ask_field(["city", "pace"]) == "ready"
    controls = interactive_control_blocks(ask_field="ready", constraints={})
    types = {block.type for block in controls}
    assert "slider" in types
    assert "toggle" in types


def test_season_from_month() -> None:
    assert season_from_month(1) == "winter"
    assert season_from_month(7) == "summer"


def test_compose_blocks_and_control_patch() -> None:
    assert _control_patch("budget_amount", 4500.0) == {"budget_amount": 4500}
    assert _control_patch("with_children", True) == {"with_children": True}
    assert _control_patch("with_pets", False) == {"with_pets": False}
    assert _control_patch("with_children", None) == {"with_children": True}
    assert _control_patch("unknown", 1) is None
    blocks = _compose_assistant_blocks(
        constraints={"city": "Ялта"},
        confirmed_fields=["city"],
        ask_field="ready",
        action_ids=["want_generate"],
        tool_context={
            "seasonal_recommendations": [
                {
                    "id": "summer_foros",
                    "title": "Летом — пляжи",
                    "body": "Рекомендую ЮБК.",
                    "accept_action": "accept_rec_summer_foros",
                },
                {"id": "bad"},
            ],
            "place_candidates": [
                {
                    "place_id": "11111111-1111-1111-1111-111111111111",
                    "title": "Форос",
                },
                {"place_id": "", "title": "skip"},
            ],
        },
        include_recommendations=False,
    )
    types = {block.type for block in blocks}
    assert "recommendation_card" not in types
    assert "place_chip" in types
    assert "actions" in types
    with_recs = _compose_assistant_blocks(
        constraints={"city": "Ялта"},
        confirmed_fields=["city"],
        ask_field="ready",
        action_ids=["want_generate"],
        tool_context={
            "seasonal_recommendations": [
                {
                    "id": "summer_foros",
                    "title": "Летом — пляжи",
                    "body": "Рекомендую ЮБК.",
                    "accept_action": "accept_rec_summer_foros",
                }
            ],
            "place_candidates": [],
        },
        include_recommendations=True,
    )
    assert "recommendation_card" in {block.type for block in with_recs}
    parsed = _try_parse_block(
        {
            "type": "recommendation_card",
            "id": "x",
            "title": "Tip",
            "body": "Body text here",
            "accept_action_id": "accept_rec_summer_foros",
        }
    )
    assert parsed is not None
    assert parsed.type == "recommendation_card"
    assert _try_parse_block({"type": "nope"}) is None
    assert (
        _try_parse_block(
            {
                "type": "slider",
                "id": "budget_amount",
                "label": "Бюджет",
                "min_value": 0,
                "max_value": 100,
                "step": 10,
                "value": 50,
                "unit": "₽",
            }
        ).type
        == "slider"
    )
    assert (
        _try_parse_block(
            {"type": "toggle", "id": "with_pets", "label": "Питомцы", "value": True}
        ).type
        == "toggle"
    )


@pytest.mark.asyncio
async def test_execute_tool_unknown_and_seasonal() -> None:
    from tourism_backend.modules.route_builder.application.tool_registry import (
        ToolCall,
        execute_tool,
    )

    class _DummySession:
        pass

    unknown = await execute_tool(
        _DummySession(),  # type: ignore[arg-type]
        ToolCall(name="drop_table", arguments={}),
        constraints={},
    )
    assert unknown.ok is False
    seasonal = await execute_tool(
        _DummySession(),  # type: ignore[arg-type]
        ToolCall(name="seasonal_recommendations", arguments={"season": "winter"}),
        constraints={},
    )
    assert seasonal.ok is True
    assert seasonal.data["season"] == "winter"


@pytest.mark.asyncio
async def test_search_places_without_city() -> None:
    from tourism_backend.modules.route_builder.application.tool_registry import (
        ToolCall,
        execute_tool,
        prefetch_context,
    )

    class _EmptySession:
        async def scalar(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def scalars(self, *_args: object, **_kwargs: object) -> object:
            class _R:
                def all(self) -> list[object]:
                    return []

            return _R()

    result = await execute_tool(
        _EmptySession(),  # type: ignore[arg-type]
        ToolCall(name="search_places", arguments={"city": ""}),
        constraints={},
    )
    assert result.ok is True
    assert result.data["places"] == []

    ctx = await prefetch_context(
        _EmptySession(),  # type: ignore[arg-type]
        constraints={},
        confirmed_fields=[],
    )
    assert "seasonal_recommendations" in ctx
    assert ctx["place_candidates"] == []

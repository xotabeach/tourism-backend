"""Extra coverage for structured turns and action builders."""

from tourism_backend.modules.route_builder.application.chat_actions import (
    build_actions_block,
    field_for_action,
    fields_touched_by_patch,
    first_missing_ask_field,
    patch_for_action,
)
from tourism_backend.modules.route_builder.application.session_service import _try_parse_block
from tourism_backend.modules.route_builder.application.structured_turn import (
    fallback_structured_turn,
    parse_structured_turn,
)


def test_parse_markdown_fenced_json() -> None:
    raw = """```json
{"assistant_text":"Ок","ask_field":"ready","action_ids":["want_generate"],"constraint_patch":{}}
```"""
    parsed = parse_structured_turn(raw, confirmed_fields=["city", "pace", "interests", "duration"])
    assert parsed is not None
    assert parsed.ask_field == "ready"
    assert parsed.action_ids == ("want_generate",)


def test_fallback_ready_and_snippet() -> None:
    ready = fallback_structured_turn(
        confirmed_fields=["city", "pace", "interests", "duration", "transport_mode", "people"],
        user_snippet="",
    )
    assert ready.ask_field == "ready"
    assert "Подбери" in ready.assistant_text or "давай" in ready.assistant_text.casefold()

    with_snippet = fallback_structured_turn(
        confirmed_fields=["city"],
        user_snippet="хочу спокойно " + ("x" * 80),
    )
    assert with_snippet.ask_field == "pace"
    assert "…" in with_snippet.assistant_text or "..." in with_snippet.assistant_text


def test_parse_rejects_empty_text_and_unknown_ask() -> None:
    assert parse_structured_turn('{"assistant_text":"","ask_field":"pace"}') is None
    parsed = parse_structured_turn(
        '{"assistant_text":"Ок","ask_field":"not_a_field","action_ids":["pace_calm"]}',
        confirmed_fields=["city"],
    )
    assert parsed is not None
    assert parsed.ask_field == "pace"
    assert parsed.action_ids == ("pace_calm",)


def test_sanitize_patch_types() -> None:
    parsed = parse_structured_turn(
        '{"assistant_text":"Ок","ask_field":"people","constraint_patch":'
        '{"people":3,"paid_ok":true,"interests_add":["море"],"budget_amount":100}}',
        confirmed_fields=["city"],
    )
    assert parsed is not None
    assert parsed.constraint_patch["people"] == 3
    assert parsed.constraint_patch["paid_ok"] is True
    assert parsed.constraint_patch["interests_add"] == ["море"]
    assert parsed.constraint_patch["budget_amount"] == 100


def test_patch_and_field_helpers() -> None:
    assert patch_for_action("transport_car") == {"transport_mode": "car"}
    assert field_for_action("want_generate") is None
    assert fields_touched_by_patch({"interests_add": ["море"], "pace": "calm"}) == [
        "interests",
        "pace",
    ]
    assert first_missing_ask_field(["city", "pace"]) == "interests"
    blocks = build_actions_block(ask_field="ready", confirmed_fields=["city", "pace"])
    assert blocks[0].actions[0]["id"] == "want_generate"


def test_build_actions_for_duration_and_people() -> None:
    duration = build_actions_block(ask_field="duration", confirmed_fields=["city"])
    people = build_actions_block(ask_field="people", confirmed_fields=["city", "pace"])
    assert {item["id"] for item in duration[0].actions} >= {"duration_d1_2", "duration_d3_5"}
    assert {item["id"] for item in people[0].actions} >= {"people_1", "people_2"}


def test_reserved_slider_toggle_blocks_parse() -> None:
    slider = _try_parse_block(
        {
            "type": "slider",
            "id": "budget",
            "label": "Бюджет",
            "min_value": 0,
            "max_value": 10000,
            "step": 500,
            "value": 2000,
        }
    )
    toggle = _try_parse_block(
        {"type": "toggle", "id": "with_pets", "label": "С питомцами", "value": True}
    )
    assert slider is not None
    assert slider.type == "slider"
    assert toggle is not None
    assert toggle.type == "toggle"

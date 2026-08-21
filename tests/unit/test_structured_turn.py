"""Unit tests for structured chat turn parsing."""

from tourism_backend.modules.route_builder.application.structured_turn import (
    fallback_structured_turn,
    parse_structured_turn,
)


def test_parse_structured_turn_allowlist() -> None:
    raw = (
        '{"assistant_text":"На машине или транспорте?",'
        '"ask_field":"transport_mode",'
        '"action_ids":["transport_car","transport_public","evil"],'
        '"constraint_patch":{"pace":"calm","password":"x"}}'
    )
    parsed = parse_structured_turn(raw, confirmed_fields=["city"])
    assert parsed is not None
    assert parsed.ask_field == "transport_mode"
    assert parsed.action_ids == ("transport_car", "transport_public")
    assert parsed.constraint_patch == {"pace": "calm"}
    assert "password" not in parsed.constraint_patch


def test_parse_structured_turn_rejects_non_json() -> None:
    assert parse_structured_turn("просто текст без json") is None


def test_fallback_structured_turn_missing_city() -> None:
    turn = fallback_structured_turn(confirmed_fields=[], user_snippet="привет")
    assert turn.ask_field == "city"
    assert "город" in turn.assistant_text.casefold() or "крым" in turn.assistant_text.casefold()

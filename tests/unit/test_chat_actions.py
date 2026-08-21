"""Unit tests for chat clarification action chips."""

from tourism_backend.modules.route_builder.application.chat_actions import (
    build_actions_block,
    clarification_action_blocks,
    known_constraints,
    merge_constraint_patch,
    normalize_action_id,
    sanitize_confirmed_fields,
)


def test_clarification_actions_follow_ask_field() -> None:
    transport = clarification_action_blocks(
        {"city": "Ялта"},
        confirmed_fields=["city"],
        ask_field="transport_mode",
    )
    ids = {item["id"] for item in transport[0].actions}
    assert "transport_car" in ids
    assert "transport_public" in ids
    assert "pace_calm" not in ids


def test_explicit_action_ids_override_defaults() -> None:
    blocks = build_actions_block(
        action_ids=["transport_car", "want_generate", "bogus", "transport_car"],
        confirmed_fields=["city"],
    )
    ids = [item["id"] for item in blocks[0].actions]
    assert ids[0] == "transport_car"
    assert "want_generate" in ids
    assert "bogus" not in ids


def test_legacy_aliases_normalize() -> None:
    assert normalize_action_id("say_mood_calm") == "pace_calm"
    assert normalize_action_id("say_more_sea") == "interest_sea"


def test_known_constraints_and_merge() -> None:
    constraints = {
        "city": "Ялта",
        "people": 2,
        "pace": "calm",
        "interests": ["природа"],
    }
    assert known_constraints(constraints, ["city"]) == {"city": "Ялта"}
    # First chat interest replaces form draft list, does not append.
    merged = merge_constraint_patch(
        constraints,
        {"interests_add": ["горы"], "pace": "active"},
        previously_confirmed=[],
    )
    assert merged["pace"] == "active"
    assert merged["interests"] == ["горы"]
    # Later interest adds onto already stated interests.
    merged_again = merge_constraint_patch(
        merged,
        {"interests_add": ["море"]},
        previously_confirmed=["interests", "pace"],
    )
    assert merged_again["interests"] == ["горы", "море"]
    assert sanitize_confirmed_fields(["city", "hack", "pace", "city"]) == [
        "city",
        "pace",
    ]


def test_form_draft_excludes_confirmed_and_placeholder_city() -> None:
    from tourism_backend.modules.route_builder.application.chat_actions import (
        form_draft_constraints,
    )

    draft = form_draft_constraints(
        {
            "city": "Крым",
            "people": 2,
            "interests": ["природа"],
            "pace": "calm",
        },
        ["pace"],
    )
    assert "city" not in draft
    assert draft["people"] == 2
    assert draft["interests"] == ["природа"]
    assert "pace" not in draft

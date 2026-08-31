"""Unit tests for cross-session preference write-back (Workstream C).

``apply_chat_preferences`` only reads/writes plain attributes on a `User`
ORM instance — constructing one directly (no session, no flush) is enough
to exercise the logic without a database.
"""

from __future__ import annotations

from tourism_backend.modules.identity.application.chat_preferences import (
    apply_chat_preferences,
)
from tourism_backend.modules.identity.infrastructure.models import User


def _user(**overrides: object) -> User:
    defaults: dict[str, object] = {
        "preferred_categories": None,
        "preferred_difficulty": None,
        "travels_with_kids": False,
        "travels_with_pets": False,
        "preferences_updated_at": None,
    }
    defaults.update(overrides)
    return User(**defaults)  # type: ignore[arg-type]


def test_nothing_confirmed_writes_nothing() -> None:
    user = _user()

    changed = apply_chat_preferences(
        user,
        constraints={"interests": ["море"], "pace": "active"},
        confirmed_fields=[],  # nothing confirmed this session
    )

    assert changed == []
    assert user.preferred_categories is None
    assert user.preferred_difficulty is None
    assert user.preferences_updated_at is None


def test_confirmed_interests_and_pace_are_written() -> None:
    user = _user()

    changed = apply_chat_preferences(
        user,
        constraints={"interests": ["горы", "история"], "pace": "active"},
        confirmed_fields=["interests", "pace"],
    )

    assert user.preferred_categories == ["горы", "история"]
    assert user.preferred_difficulty == "hard"
    assert user.preferences_updated_at is not None
    assert "интересы" in changed
    assert "предпочитаемый темп" in changed


def test_confirmed_children_and_pets_flags_are_written() -> None:
    user = _user()

    changed = apply_chat_preferences(
        user,
        constraints={"with_children": True, "with_pets": True},
        confirmed_fields=["with_children", "with_pets"],
    )

    assert user.travels_with_kids is True
    assert user.travels_with_pets is True
    assert set(changed) == {"путешествия с детьми", "путешествия с питомцами"}


def test_value_unchanged_from_profile_is_not_reported_as_a_change() -> None:
    user = _user(travels_with_kids=True)

    changed = apply_chat_preferences(
        user,
        constraints={"with_children": True},
        confirmed_fields=["with_children"],
    )

    assert changed == []
    assert user.preferences_updated_at is None


def test_unconfirmed_form_draft_is_never_written() -> None:
    """A slider/city-form draft sitting in constraints must not leak in."""
    user = _user()

    changed = apply_chat_preferences(
        user,
        constraints={"interests": ["природа"], "pace": "calm", "with_pets": True},
        confirmed_fields=["pace"],  # only pace was actually confirmed
    )

    assert user.preferred_difficulty == "easy"
    assert user.preferred_categories is None
    assert user.travels_with_pets is False
    assert changed == ["предпочитаемый темп"]

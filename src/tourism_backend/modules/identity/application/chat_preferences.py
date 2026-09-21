"""Persist AI-chat-confirmed preferences back to the user's profile.

Only ever called from the explicit `save_preferences` chat action — never
silently from a regular chat turn. Matches the rule already enforced for
catalog preferences (see
`tourism-platform/docs/2gis-personalization-offline-plan-2026-08-28.md`,
section 5.1: "не записывает preferences без явного подтверждения").
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tourism_backend.modules.identity.infrastructure.models import (
    PREFERENCE_DURATIONS,
    User,
    to_preference_categories,
)

_PACE_TO_DIFFICULTY = {"calm": "easy", "moderate": "moderate", "active": "hard"}


def apply_chat_preferences(
    user: User,
    *,
    constraints: dict[str, Any],
    confirmed_fields: list[str],
) -> list[str]:
    """Merge this session's confirmed fields into ``user``'s profile in place.

    Only fields the user actually confirmed *this* chat are considered —
    ``constraints`` may hold form-draft values that were never confirmed,
    and those must not leak into a persistent profile. Returns the
    human-readable list of what changed (Russian, for the confirmation
    reply); an empty list means nothing preference-shaped was confirmed, so
    nothing was written and ``preferences_updated_at`` is left untouched.
    """
    confirmed = set(confirmed_fields)
    changed: list[str] = []

    if "interests" in confirmed:
        # The chat's interest chips are a wider vocabulary than the profile
        # quiz; keep only what maps onto the profile's own words.
        interests = to_preference_categories(
            [str(item) for item in (constraints.get("interests") or []) if item]
        )
        if interests and interests != list(user.preferred_categories or []):
            user.preferred_categories = interests
            changed.append("интересы")

    if "pace" in confirmed:
        difficulty = _PACE_TO_DIFFICULTY.get(str(constraints.get("pace") or ""))
        if difficulty and difficulty != user.preferred_difficulty:
            user.preferred_difficulty = difficulty
            changed.append("предпочитаемый темп")

    if "duration" in confirmed:
        duration = str(constraints.get("duration") or "")
        if duration in PREFERENCE_DURATIONS and duration != user.preferred_duration:
            user.preferred_duration = duration
            changed.append("длительность маршрута")

    if "with_children" in confirmed:
        value = bool(constraints.get("with_children"))
        if value != user.travels_with_kids:
            user.travels_with_kids = value
            changed.append("путешествия с детьми")

    if "with_pets" in confirmed:
        value = bool(constraints.get("with_pets"))
        if value != user.travels_with_pets:
            user.travels_with_pets = value
            changed.append("путешествия с питомцами")

    if changed:
        user.preferences_updated_at = datetime.now(UTC)
    return changed

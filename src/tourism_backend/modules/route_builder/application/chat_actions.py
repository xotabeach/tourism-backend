"""Server-built interactive action chips for AI route chat."""

from __future__ import annotations

from typing import Any

from tourism_backend.modules.route_builder.application.schemas import ActionsBlockOut


def clarification_action_blocks(
    constraints: dict[str, Any] | None = None,
) -> list[ActionsBlockOut]:
    """Quick replies shown under clarification / greeting assistant text.

    LLM must not invent full itineraries; these chips steer the user toward
    generate (proposal card) or short preference updates.
    """
    _ = constraints
    return [
        ActionsBlockOut(
            actions=[
                {"id": "want_generate", "label": "Подбери маршрут"},
                {"id": "say_mood_calm", "label": "Хочу спокойно"},
                {"id": "say_mood_active", "label": "Хочу активно"},
                {"id": "say_more_sea", "label": "Больше моря"},
                {"id": "say_more_mountains", "label": "Больше гор"},
                {"id": "say_with_kids", "label": "С детьми"},
            ]
        )
    ]

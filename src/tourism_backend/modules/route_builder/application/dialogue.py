"""Purpose of a turn, independent of destination names and form completeness."""

import re
from typing import Literal, cast

DialogueGoal = Literal["discover", "compare", "place_info", "custom", "clarify"]
GOALS = frozenset({"discover", "compare", "place_info", "custom", "clarify"})


def valid_goal(value: object) -> DialogueGoal | None:
    if isinstance(value, str) and value in GOALS:
        return cast(DialogueGoal, value)
    return None


def fallback_goal(text: str, previous: str = "clarify") -> DialogueGoal:
    """Compatibility/outage fallback. Semantic model output is authoritative.

    These are purpose signals, never a list of towns. Short follow-ups retain
    the active purpose; a factual question interrupts a planning questionnaire.
    """
    folded = text.casefold().replace("ё", "е")
    if re.search(r"сравн|чем\s+отлич|какой\s+из|который\s+из", folded):
        return "compare"
    if re.search(r"(?:собер|постро|состав).{0,30}(?:свой|собствен|подроб|по\s+дням|план)", folded):
        return "custom"
    if re.search(
        r"расскажи|истори[яю]|сколько\s+(?:стоит|идти)|когда\s+откры|что\s+такое|как\s+добрат",
        folded,
    ):
        return "place_info"
    if re.search(
        r"подобра|подбер|посовет|предл[ао]г|рекоменд|куда\s+по|что\s+посмотр|"
        r"хочу.{0,45}(?:прогул|мор[еяю]|гор[ыах]|природ|маршрут)|вариант",
        folded,
    ):
        return "discover"
    return valid_goal(previous) or "clarify"

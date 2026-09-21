"""The profile interest words and how older words are folded into them."""

from __future__ import annotations

from tourism_backend.modules.identity.infrastructure.models import (
    PREFERENCE_CATEGORIES,
    to_preference_categories,
)
from tourism_backend.modules.route_builder.application.scoring import (
    categories_for_interest,
)


def test_every_profile_word_steers_place_categories() -> None:
    # A profile interest that maps to no place category would be silently
    # ignored by recommendations and the route matcher.
    for word in PREFERENCE_CATEGORIES:
        assert categories_for_interest(word), word


def test_old_words_fold_into_the_new_ones_in_order_without_duplicates() -> None:
    assert to_preference_categories(["Море", "Горы", "Еда", "Лес"]) == [
        "Природа",
        "Смотровые",
        "Гастрономия",
    ]
    assert to_preference_categories(["Экстрим", "с детьми"]) == ["Семейное"]
    assert to_preference_categories(list(PREFERENCE_CATEGORIES)) == list(PREFERENCE_CATEGORIES)

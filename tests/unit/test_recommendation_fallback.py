"""An exhausted personal deck must not leave the swiper blank.

With a small catalogue an active user quickly saves or skips nearly every
route, which emptied the deck entirely (observed in production: 22 favourites
+ 8 skips out of 27 eligible routes).
"""

from __future__ import annotations


def test_catalog_fallback_is_an_allowed_explanation_code() -> None:
    from tourism_backend.modules.recommendations.application.service import (
        _EXPLANATION_CODES,
        _explanation_code,
    )

    assert "catalog_fallback" in _EXPLANATION_CODES
    assert _explanation_code("catalog_fallback") == "catalog_fallback"


def test_unknown_explanation_code_still_degrades_to_cold_start() -> None:
    from tourism_backend.modules.recommendations.application.service import _explanation_code

    assert _explanation_code("not_a_code") == "cold_start"

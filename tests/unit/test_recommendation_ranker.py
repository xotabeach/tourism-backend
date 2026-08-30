"""Pure tests for recommendation ranker v1."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from tourism_backend.modules.recommendations.application.policy import (
    RANKER_VERSION,
    deck_date_for,
    season_for,
)
from tourism_backend.modules.recommendations.application.ranker import (
    RecommendationCandidate,
    RecommendationProfile,
    eligible_candidates,
    primary_category,
    rerank_for_diversity,
    score_candidates,
)


def _id(n: int) -> UUID:
    return UUID(int=n)


def _candidate(
    n: int,
    *,
    region: int = 1,
    categories: frozenset[str] = frozenset({"beach"}),
    difficulty: str | None = "easy",
    kids: bool | None = None,
    pets: bool | None = None,
    favorites: int = 0,
    quality: str = "unknown",
    created_at: datetime | None = None,
    seasonality: tuple[str, ...] = (),
) -> RecommendationCandidate:
    return RecommendationCandidate(
        route_id=_id(n),
        region_id=_id(region),
        category_slugs=categories,
        difficulty=difficulty,
        suitable_for_children=kids,
        pets_allowed=pets,
        created_at=created_at or datetime(2026, 8, 1, tzinfo=UTC),
        favorite_count=favorites,
        seasonality=seasonality,
        quality_status=quality,
    )


def test_deck_date_uses_moscow_calendar_not_utc() -> None:
    as_of = datetime(2026, 8, 28, 22, 30, tzinfo=UTC)
    assert deck_date_for(as_of).isoformat() == "2026-08-29"
    assert season_for(as_of) == "summer"
    assert RANKER_VERSION == "v1"


def test_kids_hard_filter_drops_explicit_false_keeps_unknown() -> None:
    profile = RecommendationProfile(travels_with_kids=True)
    kept = eligible_candidates(
        (
            _candidate(1, kids=False),
            _candidate(2, kids=None),
            _candidate(3, kids=True),
        ),
        profile,
    )
    assert {item.route_id for item in kept} == {_id(2), _id(3)}


def test_unusable_and_favorite_and_skip_are_hard_exclusions() -> None:
    skipped = _id(2)
    favorite = _id(3)
    profile = RecommendationProfile(
        skipped_route_ids=frozenset({skipped}),
        favorite_route_ids=frozenset({favorite}),
    )
    kept = eligible_candidates(
        (
            _candidate(1, quality="unusable"),
            _candidate(2),
            _candidate(3),
            _candidate(4, quality="verified_with_warnings"),
        ),
        profile,
    )
    assert [item.route_id for item in kept] == [_id(4)]


def test_two_category_views_cannot_exist_because_v1_does_not_ingest_views() -> None:
    """A view is not a ranker input; two mountain views cannot become a filter."""

    profile = RecommendationProfile()
    scored = score_candidates(
        (
            _candidate(1, categories=frozenset({"mountain"})),
            _candidate(2, categories=frozenset({"beach"})),
        ),
        profile,
        season="summer",
        as_of=datetime(2026, 8, 29, tzinfo=UTC),
    )
    codes = {item.candidate.route_id: item.explanation_code for item in scored}
    assert codes[_id(1)] == "cold_start"
    assert codes[_id(2)] == "cold_start"


def test_explicit_sea_preference_ranks_beach_above_unrelated_trail() -> None:
    profile = RecommendationProfile(preferred_categories=frozenset({"Море"}))
    as_of = datetime(2026, 8, 29, tzinfo=UTC)
    scored = score_candidates(
        (
            _candidate(1, categories=frozenset({"trail", "cave"}), favorites=50),
            _candidate(2, categories=frozenset({"beach"}), favorites=0),
        ),
        profile,
        season="summer",
        as_of=as_of,
    )
    assert scored[0].candidate.route_id == _id(2)
    assert scored[0].explanation_code == "matches_interest"
    assert all(0.0 <= item.score <= 1.0 for item in scored)


def test_diversity_caps_category_share_when_catalog_is_wide_enough() -> None:
    mountains = [
        _candidate(n, categories=frozenset({"mountain"}), region=1, favorites=20 - n)
        for n in range(1, 13)
    ]
    beaches = [
        _candidate(n, categories=frozenset({"beach"}), region=2, favorites=1) for n in range(20, 32)
    ]
    profile = RecommendationProfile(preferred_categories=frozenset({"Горы"}))
    scored = score_candidates(
        (*mountains, *beaches),
        profile,
        season="summer",
        as_of=datetime(2026, 8, 29, tzinfo=UTC),
    )
    deck = rerank_for_diversity(scored, deck_size=16)
    assert len({item.candidate.route_id for item in deck}) == len(deck)
    assert len(deck) >= 8
    mountain_count = sum(
        1 for item in deck if primary_category(item.candidate.category_slugs) == "mountain"
    )
    assert mountain_count <= 6
    assert any(primary_category(item.candidate.category_slugs) == "beach" for item in deck)


def test_exploration_slot_appears_when_preferred_theme_is_narrow() -> None:
    preferred = [_candidate(n, categories=frozenset({"mountain"})) for n in range(1, 10)]
    other = _candidate(50, categories=frozenset({"winery"}))
    profile = RecommendationProfile(preferred_categories=frozenset({"Горы"}))
    scored = score_candidates(
        (*preferred, other),
        profile,
        season="summer",
        as_of=datetime(2026, 8, 29, tzinfo=UTC),
    )
    deck = rerank_for_diversity(scored, deck_size=8)
    assert any(item.candidate.route_id == _id(50) and item.exploration for item in deck)


def test_skip_cooldown_is_finite_not_a_lifetime_ban() -> None:
    route = _candidate(1)
    banned = eligible_candidates(
        (route,),
        RecommendationProfile(skipped_route_ids=frozenset({_id(1)})),
    )
    restored = eligible_candidates((route,), RecommendationProfile())
    assert banned == []
    assert restored == [route]


def test_fresh_route_beats_stale_when_everything_else_is_equal() -> None:
    as_of = datetime(2026, 8, 29, tzinfo=UTC)
    scored = score_candidates(
        (
            _candidate(1, created_at=as_of - timedelta(days=400), favorites=0),
            _candidate(2, created_at=as_of - timedelta(days=2), favorites=0),
        ),
        RecommendationProfile(),
        season="summer",
        as_of=as_of,
    )
    assert scored[0].candidate.route_id == _id(2)
    assert scored[0].explanation_code in {"fresh_route", "cold_start"}


def _profile() -> RecommendationProfile:
    return RecommendationProfile()


def test_single_region_catalogue_fills_the_whole_deck() -> None:
    """Crimea-only catalogue: the region cap must not halve the deck.

    With one region the cap can never balance anything — it only truncated
    the deck to max_region_share of its size, which is why a user with
    plenty of eligible routes still saw a short deck.
    """
    candidates = [
        _candidate(n, region=1, categories=frozenset({f"cat-{n % 6}"})) for n in range(1, 21)
    ]
    scored = score_candidates(
        candidates, _profile(), season="лето", as_of=datetime(2026, 8, 30, tzinfo=UTC)
    )

    deck = rerank_for_diversity(scored, deck_size=16)

    assert len(deck) == 16


def test_multi_region_catalogue_still_balances_regions() -> None:
    candidates = [
        _candidate(
            n,
            region=1 if n <= 18 else 2,
            categories=frozenset({f"cat-{n % 6}"}),
        )
        for n in range(1, 21)
    ]
    scored = score_candidates(
        candidates, _profile(), season="лето", as_of=datetime(2026, 8, 30, tzinfo=UTC)
    )

    deck = rerank_for_diversity(scored, deck_size=16)

    crowded = sum(1 for item in deck if item.candidate.region_id == _id(1))
    assert crowded <= 8

"""Spec 19: who counts towards popularity, how much, and who earns the badge."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from tourism_backend.modules.popularity.policy import (
    BADGE_MIN_PEOPLE,
    DECAY_HALF_LIFE_DAYS,
    ROUTE_WEIGHT_COMPLETED,
    ROUTE_WEIGHT_FAVORITE,
    ROUTE_WEIGHT_STARTED,
    WINDOW_DAYS,
)
from tourism_backend.modules.popularity.scoring import (
    Action,
    blend_place,
    decay,
    entity_scores,
    percentile_scores,
    popular_places,
    popular_routes,
    top_share,
)
from tourism_backend.modules.routes.application.service import route_badge

NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)


def _action(user: UUID, entity: UUID, weight: float, days_ago: float = 0.0) -> Action:
    return Action(user, entity, weight, NOW - timedelta(days=days_ago))


def test_decay_halves_at_half_life_and_stops_at_window() -> None:
    assert decay(0) == 1.0
    assert decay(DECAY_HALF_LIFE_DAYS) == pytest.approx(0.5)
    assert decay(WINDOW_DAYS) > 0
    assert decay(WINDOW_DAYS + 1) == 0.0
    # A clock a little ahead of the server is "now", not an error.
    assert decay(-0.5) == 1.0


def test_people_outside_the_eligible_set_do_not_count() -> None:
    """The service account, team accounts and new accounts are simply absent (D45)."""

    route = uuid4()
    walker, system_account = uuid4(), uuid4()
    scores = entity_scores(
        [
            _action(walker, route, ROUTE_WEIGHT_COMPLETED),
            _action(system_account, route, ROUTE_WEIGHT_COMPLETED),
        ],
        now=NOW,
        eligible_user_ids={walker},
    )
    assert scores[route].people == 1
    assert scores[route].raw == pytest.approx(ROUTE_WEIGHT_COMPLETED)

    nobody = entity_scores(
        [_action(system_account, route, ROUTE_WEIGHT_COMPLETED)],
        now=NOW,
        eligible_user_ids=set(),
    )
    assert nobody == {}


def test_one_person_gives_their_strongest_action_not_the_sum() -> None:
    route, walker = uuid4(), uuid4()
    scores = entity_scores(
        [
            _action(walker, route, ROUTE_WEIGHT_FAVORITE),
            _action(walker, route, ROUTE_WEIGHT_STARTED),
            _action(walker, route, ROUTE_WEIGHT_COMPLETED),
            _action(walker, route, ROUTE_WEIGHT_COMPLETED),
        ],
        now=NOW,
        eligible_user_ids={walker},
    )
    assert scores[route].people == 1
    assert scores[route].raw == pytest.approx(ROUTE_WEIGHT_COMPLETED)


def test_fresh_action_outweighs_an_old_one_and_the_window_cuts_off() -> None:
    fresh, old, ancient = uuid4(), uuid4(), uuid4()
    walker = uuid4()
    scores = entity_scores(
        [
            _action(walker, fresh, ROUTE_WEIGHT_COMPLETED, days_ago=1),
            _action(walker, old, ROUTE_WEIGHT_COMPLETED, days_ago=80),
            _action(walker, ancient, ROUTE_WEIGHT_COMPLETED, days_ago=WINDOW_DAYS + 5),
        ],
        now=NOW,
        eligible_user_ids={walker},
    )
    assert scores[fresh].raw > scores[old].raw
    assert ancient not in scores


def test_percentile_scores_rank_ties_together_and_skip_zero() -> None:
    low, mid_a, mid_b, top, empty = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    result = percentile_scores({low: 1.0, mid_a: 2.0, mid_b: 2.0, top: 5.0, empty: 0.0})
    assert empty not in result
    assert result[top] == 100.0
    assert result[mid_a] == result[mid_b]
    assert result[low] < result[mid_a] < result[top]
    assert percentile_scores({}) == {}


def test_blend_place_moves_from_outside_fame_to_the_app_with_people() -> None:
    assert blend_place(external=None, app=40.0, people=3) == 40.0
    assert blend_place(external=80.0, app=0.0, people=0) == 80.0
    few = blend_place(external=80.0, app=20.0, people=2)
    many = blend_place(external=80.0, app=20.0, people=200)
    assert 20.0 < many < few < 80.0


def test_top_share_is_a_tenth_of_everything_published() -> None:
    ids = [uuid4() for _ in range(30)]
    scores = {entity_id: float(index + 1) for index, entity_id in enumerate(ids)}
    # 30 scored out of 100 published: the badge quota is ten, not three.
    assert top_share(scores, population=100) == set(ids[-10:])
    # Too few published for a tenth to be a whole route: nobody.
    assert top_share(scores, population=9) == set()
    assert top_share({ids[0]: 0.0}, population=100) == set()


def test_top_share_leaves_out_a_tie_at_the_cut_off() -> None:
    first, tied_a, tied_b = uuid4(), uuid4(), uuid4()
    scores = {first: 9.0, tied_a: 5.0, tied_b: 5.0}
    assert top_share(scores, population=20) == {first}


def test_ten_new_accounts_with_one_favourite_give_no_badge() -> None:
    """Spec 19, the readiness check: new accounts are not eligible at all."""

    route = uuid4()
    newcomers = [uuid4() for _ in range(10)]
    scores = entity_scores(
        [_action(user, route, ROUTE_WEIGHT_FAVORITE) for user in newcomers],
        now=NOW,
        eligible_user_ids=set(),
    )
    popularity = percentile_scores({rid: score.raw for rid, score in scores.items()})
    people = {rid: score.people for rid, score in scores.items()}
    assert popular_routes(popularity, people, population=50) == set()


def test_route_badge_needs_enough_different_people() -> None:
    crowded, quiet = uuid4(), uuid4()
    popularity = {crowded: 100.0, quiet: 90.0}
    people = {crowded: BADGE_MIN_PEOPLE, quiet: BADGE_MIN_PEOPLE - 1}
    assert popular_routes(popularity, people, population=20) == {crowded}


def test_place_badge_from_day_one_only_with_outside_fame() -> None:
    famous, app_only_quiet, app_only_crowded = uuid4(), uuid4(), uuid4()
    final = {famous: 95.0, app_only_quiet: 99.0, app_only_crowded: 97.0}
    people = {app_only_quiet: 1, app_only_crowded: BADGE_MIN_PEOPLE}
    assert popular_places(final, people, {famous}, population=30) == {
        famous,
        app_only_crowded,
    }


def test_route_badge_prefers_popular_over_editors_choice() -> None:
    def route(**fields: object) -> SimpleNamespace:
        return SimpleNamespace(**{"is_popular": False, "source": "user_created", **fields})

    assert route_badge(route(is_popular=True, source="editorial")) == "popular"  # type: ignore[arg-type]
    assert route_badge(route(source="editorial")) == "editors_choice"  # type: ignore[arg-type]
    assert route_badge(route()) is None  # type: ignore[arg-type]

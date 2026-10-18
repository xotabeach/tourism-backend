"""The budget as one of four levels (BACKEND-57, spec 33 D13)."""

from tourism_backend.modules.route_builder.application.budget import (
    budget_level_label,
    normalize_budget,
)
from tourism_backend.modules.route_builder.application.schemas import RouteMatchParamsIn
from tourism_backend.modules.route_builder.application.session_service import price_level_label

BOUNDS = (1500, 4000, 10000)


def test_old_sum_becomes_a_level_and_zero_stays_free_only() -> None:
    assert normalize_budget(RouteMatchParamsIn(budget_amount=3000), BOUNDS).budget_level == 2
    assert normalize_budget(RouteMatchParamsIn(budget_amount=1500), BOUNDS).budget_level == 1
    assert normalize_budget(RouteMatchParamsIn(budget_amount=50_000), BOUNDS).budget_level == 4
    zero = normalize_budget(RouteMatchParamsIn(budget_amount=0), BOUNDS)
    assert zero.budget_level is None
    assert zero.paid_ok is False
    none = normalize_budget(RouteMatchParamsIn(), BOUNDS)
    assert none.budget_level is None
    assert none.paid_ok is None


def test_level_gives_the_picker_its_upper_bound() -> None:
    comfort = normalize_budget(RouteMatchParamsIn(budget_level=3, budget_amount=500), BOUNDS)
    assert comfort.budget_level == 3
    assert comfort.budget_amount == 10000
    no_limit = normalize_budget(RouteMatchParamsIn(budget_level=4), BOUNDS)
    assert no_limit.budget_amount is None


def test_labels() -> None:
    assert budget_level_label(2) == "Средне"
    assert budget_level_label(None) is None
    assert price_level_label(3) == "₽₽₽ · Комфорт"
    assert price_level_label(None) is None
    assert price_level_label(None, free=True) == "Бесплатно"


def test_model_sum_is_bounded_and_clears_a_level() -> None:
    from tourism_backend.modules.route_builder.application.structured_turn import _sanitize_patch

    patch = _sanitize_patch({"budget_amount": 99_999_999})
    assert patch["budget_amount"] == 1_000_000
    assert patch["budget_level"] is None
    assert _sanitize_patch({"budget_amount": -10})["budget_amount"] == 0

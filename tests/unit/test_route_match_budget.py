"""Budget input of the match form (FRONTEND-46)."""

import pytest
from pydantic import ValidationError

from tourism_backend.modules.route_builder.application.schemas import (
    MAX_BUDGET_AMOUNT,
    RouteMatchParamsIn,
)


def test_an_oversized_budget_is_capped_instead_of_rejected() -> None:
    # «100000000» used to fail the whole match with «Request validation failed».
    params = RouteMatchParamsIn.model_validate({"budget_amount": 100_000_000})
    assert params.budget_amount == MAX_BUDGET_AMOUNT


def test_an_ordinary_budget_is_kept() -> None:
    assert RouteMatchParamsIn.model_validate({"budget_amount": 15_000}).budget_amount == 15_000


def test_a_negative_budget_is_still_rejected() -> None:
    with pytest.raises(ValidationError):
        RouteMatchParamsIn.model_validate({"budget_amount": -1})

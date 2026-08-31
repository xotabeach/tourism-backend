"""Unit tests for the Workstream A TSP stop-order wiring in generate_service.

Exercises ``_maybe_optimize_stop_order`` in isolation with a fake TspProvider
and a monkeypatched waypoint loader, so no database is required — the risky,
novel behaviour here is the reorder/fallback logic, not the DB round-trip
already covered elsewhere.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tourism_backend.modules.route_builder.application import generate_service
from tourism_backend.modules.route_builder.application.place_picker import PickedPlace
from tourism_backend.modules.route_builder.application.routing import RouteWaypoint
from tourism_backend.modules.route_builder.application.schemas import RouteMatchParamsIn
from tourism_backend.modules.route_builder.application.tsp import TspError, TspOrderResult


def _place(name: str) -> PickedPlace:
    return PickedPlace(
        place_id=uuid4(),
        name=name,
        short_description=None,
        recommended_visit_minutes=45,
    )


def _params() -> RouteMatchParamsIn:
    return RouteMatchParamsIn.model_validate(
        {"city": "Ялта", "duration": "d3_5", "pace": "moderate", "transport_mode": "walk"}
    )


class _FakeProvider:
    def __init__(
        self,
        *,
        result: TspOrderResult | None = None,
        error: TspError | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.calls = 0

    async def optimize_order(
        self, *, waypoints: list[RouteWaypoint], transport_mode: str
    ) -> TspOrderResult:
        self.calls += 1
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


@pytest.mark.asyncio
async def test_two_places_skip_optimization_without_touching_provider_or_db() -> None:
    places = [_place("A"), _place("B")]
    provider = _FakeProvider(result=TspOrderResult(order=(0, 1), optimized=True))

    result = await generate_service._maybe_optimize_stop_order(
        session=None,  # type: ignore[arg-type]  # must never be dereferenced for < 3 places
        places=places,
        params=_params(),
        provider=provider,
    )

    assert result == places
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_provider_result_reorders_places(monkeypatch: pytest.MonkeyPatch) -> None:
    places = [_place("A"), _place("B"), _place("C")]

    async def fake_waypoints(session, given_places):  # noqa: ANN001, ARG001
        return [RouteWaypoint(lng=0.0, lat=0.0) for _ in given_places]

    monkeypatch.setattr(generate_service, "_waypoints_for_places", fake_waypoints)
    provider = _FakeProvider(result=TspOrderResult(order=(0, 2, 1), optimized=True))

    result = await generate_service._maybe_optimize_stop_order(
        session=None,  # type: ignore[arg-type]
        places=places,
        params=_params(),
        provider=provider,
    )

    assert [place.name for place in result] == ["A", "C", "B"]
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_unoptimized_result_keeps_original_order(monkeypatch: pytest.MonkeyPatch) -> None:
    places = [_place("A"), _place("B"), _place("C")]

    async def fake_waypoints(session, given_places):  # noqa: ANN001, ARG001
        return [RouteWaypoint(lng=0.0, lat=0.0) for _ in given_places]

    monkeypatch.setattr(generate_service, "_waypoints_for_places", fake_waypoints)
    provider = _FakeProvider(result=TspOrderResult(order=(0, 1, 2), optimized=False))

    result = await generate_service._maybe_optimize_stop_order(
        session=None,  # type: ignore[arg-type]
        places=places,
        params=_params(),
        provider=provider,
    )

    assert result == places


@pytest.mark.asyncio
async def test_provider_failure_keeps_original_order(monkeypatch: pytest.MonkeyPatch) -> None:
    places = [_place("A"), _place("B"), _place("C")]

    async def fake_waypoints(session, given_places):  # noqa: ANN001, ARG001
        return [RouteWaypoint(lng=0.0, lat=0.0) for _ in given_places]

    monkeypatch.setattr(generate_service, "_waypoints_for_places", fake_waypoints)
    provider = _FakeProvider(error=TspError("tsp_timeout", "boom"))

    result = await generate_service._maybe_optimize_stop_order(
        session=None,  # type: ignore[arg-type]
        places=places,
        params=_params(),
        provider=provider,
    )

    assert result == places


@pytest.mark.asyncio
async def test_missing_provider_falls_back_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """tsp_provider=2gis without a configured key must not break generation."""
    places = [_place("A"), _place("B"), _place("C")]

    def fake_get_tsp_provider(settings):  # noqa: ANN001, ARG001
        raise RuntimeError("TWO_GIS_HTTP_API_KEY is required for 2GIS TSP")

    monkeypatch.setattr(generate_service, "get_tsp_provider", fake_get_tsp_provider)

    result = await generate_service._maybe_optimize_stop_order(
        session=None,  # type: ignore[arg-type]
        places=places,
        params=_params(),
    )

    assert result == places

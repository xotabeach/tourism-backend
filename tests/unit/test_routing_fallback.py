"""_route_places falls back to a synthetic route when 2GIS is unavailable."""

from __future__ import annotations

from uuid import uuid4

import pytest

from tourism_backend.api.errors import AppError
from tourism_backend.config import Settings
from tourism_backend.modules.route_builder.application import generate_service
from tourism_backend.modules.route_builder.application.place_picker import PickedPlace
from tourism_backend.modules.route_builder.application.routing import (
    RouteWaypoint,
    RoutingError,
)
from tourism_backend.modules.route_builder.application.schemas import RouteMatchParamsIn


class _AlwaysFailingProvider:
    def __init__(self, code: str) -> None:
        self._code = code

    async def route(self, **_: object) -> object:
        raise RoutingError(self._code, "boom")


def _places() -> list[PickedPlace]:
    return [
        PickedPlace(
            place_id=uuid4(),
            name="A",
            short_description=None,
            recommended_visit_minutes=None,
        ),
        PickedPlace(
            place_id=uuid4(),
            name="B",
            short_description=None,
            recommended_visit_minutes=None,
        ),
    ]


def _params() -> RouteMatchParamsIn:
    return RouteMatchParamsIn(city="Ялта", pace="calm")


@pytest.fixture(autouse=True)
def _stub_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_waypoints(_session: object, places: list[PickedPlace]) -> list[RouteWaypoint]:
        return [
            RouteWaypoint(lng=34.10 + index * 0.01, lat=44.50 + index * 0.01)
            for index, _ in enumerate(places)
        ]

    async def fake_road_events(_session: object, _places: list[PickedPlace]) -> tuple[()]:
        return ()

    monkeypatch.setattr(generate_service, "_waypoints_for_places", fake_waypoints)
    monkeypatch.setattr(generate_service, "_road_events_for_places", fake_road_events)


@pytest.mark.asyncio
async def test_provider_unavailable_falls_back_to_synthetic_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        generate_service,
        "get_settings",
        lambda: Settings(routing_provider="2gis"),
    )
    monkeypatch.setattr(
        generate_service,
        "get_routing_provider",
        lambda _settings: _AlwaysFailingProvider("routing_timeout"),
    )

    result = await generate_service._route_places(
        session=object(),  # unused: lookups are stubbed above
        places=_places(),
        params=_params(),
    )

    assert result.synthetic is True
    assert result.provider == "stub"


@pytest.mark.asyncio
async def test_invalid_request_does_not_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        generate_service,
        "get_settings",
        lambda: Settings(routing_provider="2gis"),
    )
    monkeypatch.setattr(
        generate_service,
        "get_routing_provider",
        lambda _settings: _AlwaysFailingProvider("routing_request_invalid"),
    )

    with pytest.raises(AppError) as error:
        await generate_service._route_places(
            session=object(),
            places=_places(),
            params=_params(),
        )

    assert error.value.code == "routing_request_invalid"

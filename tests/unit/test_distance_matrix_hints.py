"""Unit tests for the Workstream A distance-matrix travel-hint wiring.

Exercises ``tool_registry._attach_travel_hints`` in isolation with a fake
session/provider, so no database is required — the risky, novel behaviour
here is the coordinate lookup + best-effort attach/fallback, not the DB
round-trip pattern already covered elsewhere.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from tourism_backend.config import Settings
from tourism_backend.modules.route_builder.application import tool_registry
from tourism_backend.modules.route_builder.application.distance_matrix import (
    DistanceMatrixEntry,
    DistanceMatrixError,
    DistanceMatrixResult,
)


class _FakeResult:
    def __init__(self, rows: list[tuple[object, float, float]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[object, float, float]]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: list[tuple[object, float, float]]) -> None:
        self._rows = rows

    async def execute(self, _stmt: object) -> _FakeResult:
        return _FakeResult(self._rows)


class _ExplodingSession:
    """A DB touch here would mean the guard failed to skip early."""

    async def execute(self, _stmt: object) -> None:
        raise AssertionError("must not query the DB when travel hints are skipped")


class _FakeProvider:
    def __init__(
        self,
        *,
        result: DistanceMatrixResult | None = None,
        error: DistanceMatrixError | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.calls = 0

    async def compute(
        self, *, sources: object, targets: object, transport_mode: object
    ) -> DistanceMatrixResult:
        self.calls += 1
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _place(place_id: object) -> object:
    return SimpleNamespace(id=place_id)


@pytest.mark.asyncio
async def test_stub_provider_skips_without_touching_the_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tool_registry, "get_settings", lambda: Settings(distance_matrix_provider="stub")
    )
    place_id = uuid4()
    places = [{"place_id": str(place_id)}]

    await tool_registry._attach_travel_hints(
        _ExplodingSession(),
        places=places,
        source_places=[_place(place_id)],
        near_lat=44.5,
        near_lng=34.1,
        transport_mode="walk",
    )

    assert places == [{"place_id": str(place_id)}]


@pytest.mark.asyncio
async def test_successful_lookup_attaches_duration_and_distance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tool_registry, "get_settings", lambda: Settings(distance_matrix_provider="2gis")
    )
    place_id = uuid4()
    session = _FakeSession(rows=[(place_id, 34.11, 44.51)])
    provider = _FakeProvider(
        result=DistanceMatrixResult(
            entries=(
                DistanceMatrixEntry(
                    source_index=0, target_index=0, distance_meters=850, duration_seconds=640
                ),
            )
        )
    )
    monkeypatch.setattr(tool_registry, "get_distance_matrix_provider", lambda _settings: provider)
    places = [{"place_id": str(place_id)}]

    await tool_registry._attach_travel_hints(
        session,
        places=places,
        source_places=[_place(place_id)],
        near_lat=44.5,
        near_lng=34.1,
        transport_mode="walk",
    )

    assert places[0]["travel_duration_min"] == 11
    assert places[0]["travel_distance_m"] == 850
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_provider_failure_leaves_places_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tool_registry, "get_settings", lambda: Settings(distance_matrix_provider="2gis")
    )
    place_id = uuid4()
    session = _FakeSession(rows=[(place_id, 34.11, 44.51)])
    provider = _FakeProvider(error=DistanceMatrixError("distance_matrix_timeout", "boom"))
    monkeypatch.setattr(tool_registry, "get_distance_matrix_provider", lambda _settings: provider)
    places = [{"place_id": str(place_id)}]

    await tool_registry._attach_travel_hints(
        session,
        places=places,
        source_places=[_place(place_id)],
        near_lat=44.5,
        near_lng=34.1,
        transport_mode="walk",
    )

    assert places == [{"place_id": str(place_id)}]


@pytest.mark.asyncio
async def test_missing_provider_key_skips_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tool_registry, "get_settings", lambda: Settings(distance_matrix_provider="2gis")
    )

    def fake_get_provider(_settings: object) -> object:
        raise RuntimeError("TWO_GIS_HTTP_API_KEY is required for 2GIS distance matrix")

    monkeypatch.setattr(tool_registry, "get_distance_matrix_provider", fake_get_provider)
    place_id = uuid4()
    places = [{"place_id": str(place_id)}]

    await tool_registry._attach_travel_hints(
        _ExplodingSession(),
        places=places,
        source_places=[_place(place_id)],
        near_lat=44.5,
        near_lng=34.1,
        transport_mode="walk",
    )

    assert places == [{"place_id": str(place_id)}]

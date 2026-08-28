"""Routing snapshot fingerprint and contract safety tests."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tourism_backend.modules.route_execution.application.retention import (
    MAX_RETENTION_DAYS,
    eligible_snapshot_ids,
    purge_routing_snapshots,
    retention_cutoff,
)
from tourism_backend.modules.route_execution.application.routing_snapshot import (
    _geometry_from_geojson,
    _line_geometry,
    ensure_routing_snapshot,
    routing_snapshot_fingerprint,
    routing_snapshot_out,
)
from tourism_backend.modules.route_execution.application.schemas import (
    RouteExecutionRoutingOut,
)
from tourism_backend.modules.routes.infrastructure.models import Route


def _route(*, accessibility: dict[str, object] | None = None) -> Route:
    now = datetime.now(UTC)
    return Route(
        id=uuid4(),
        transport_mode="walking",
        distance_meters=1_200,
        estimated_duration_minutes=45,
        difficulty="moderate",
        suitable_for_children=True,
        pets_allowed=False,
        seasonality=["summer"],
        accessibility=accessibility,
        updated_at=now,
    )


def test_fingerprint_is_stable_but_changes_when_stop_or_geometry_changes() -> None:
    route = _route(
        accessibility={
            "travel_pace": "moderate",
            "routing": {"provider": "2gis", "quality_status": "verified"},
        }
    )
    stops = [(uuid4(), 1, uuid4()), (uuid4(), 2, uuid4())]
    first = routing_snapshot_fingerprint(
        route,
        geometry_wkt="LINESTRING(34 44, 34.1 44.1)",
        stop_signature=stops,
    )
    assert first == routing_snapshot_fingerprint(
        route,
        geometry_wkt="LINESTRING(34 44, 34.1 44.1)",
        stop_signature=stops,
    )
    assert first != routing_snapshot_fingerprint(
        route,
        geometry_wkt="LINESTRING(34 44, 34.2 44.2)",
        stop_signature=stops,
    )
    assert first != routing_snapshot_fingerprint(
        route,
        geometry_wkt="LINESTRING(34 44, 34.1 44.1)",
        stop_signature=stops[:-1],
    )


def test_geometry_helpers_fail_closed_and_bound_coordinates() -> None:
    assert _line_geometry(None) is None
    assert _line_geometry("POINT(34 44)") is None
    assert _line_geometry("LINESTRING(34 44, 34.1 44.1)") is not None
    assert _line_geometry("LINESTRING Z (34 44 10, 34.1 44.1 12)") is not None
    assert _line_geometry("LINESTRING(999 44, 34.1 44.1)") is None
    assert _geometry_from_geojson("not-json") is None
    assert _geometry_from_geojson('{"type":"Point","coordinates":[34,44]}') is None
    assert (
        _geometry_from_geojson('{"type":"LineString","coordinates":[[34,44],[34.1,44.1]]}')
        is not None
    )
    assert (
        _geometry_from_geojson('{"type":"LineString","coordinates":[[999,44],[34.1,44.1]]}') is None
    )


class _FakeSession:
    def __init__(self, *, latest: object | None = None) -> None:
        self.latest = latest
        self.added: list[object] = []
        self.scalar_calls = 0

    async def scalar(self, _statement: object) -> object | None:
        self.scalar_calls += 1
        if self.scalar_calls == 1:
            return "LINESTRING(34 44, 34.1 44.1)"
        return self.latest

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_ensure_snapshot_reuses_matching_revision_and_appends_changed_one() -> None:
    route = _route(
        accessibility={
            "travel_pace": "calm",
            "routing": {
                "provider": "2gis",
                "quality_status": "verified_with_warnings",
                "warnings": ["surface_unknown", 4],
                "road_types": ["paved", None],
                "movement_duration_seconds": 600,
                "total_duration_seconds": 900,
                "max_road_angle_degrees": 12.5,
            },
        }
    )
    stops = [(uuid4(), 1, uuid4()), (uuid4(), 2, uuid4())]
    existing = SimpleNamespace(
        fingerprint=routing_snapshot_fingerprint(
            route,
            geometry_wkt="LINESTRING(34 44, 34.1 44.1)",
            stop_signature=stops,
        ),
        revision=3,
    )
    reused_session = _FakeSession(latest=existing)
    reused = await ensure_routing_snapshot(reused_session, route=route, stop_signature=stops)
    assert reused is existing
    assert reused_session.added == []

    new_session = _FakeSession(latest=SimpleNamespace(fingerprint="old", revision=3))
    created = await ensure_routing_snapshot(new_session, route=route, stop_signature=stops)
    assert created.revision == 4
    assert created.provider == "2gis"
    assert created.quality_status == "verified_with_warnings"
    assert created.warnings == ["surface_unknown"]
    assert created.road_types == ["paved"]
    assert created.geometry is not None
    assert new_session.added == [created]


@pytest.mark.asyncio
async def test_snapshot_output_redacts_unknown_quality_and_invalid_geometry() -> None:
    snapshot_id = uuid4()
    snapshot = SimpleNamespace(
        id=snapshot_id,
        revision=1,
        captured_at=datetime.now(UTC),
        route_updated_at=None,
        provider="2gis",
        provider_version=None,
        transport_mode="walking",
        distance_meters=100,
        movement_duration_seconds=60,
        visit_duration_minutes=0,
        transfer_duration_seconds=0,
        buffer_duration_seconds=0,
        total_duration_seconds=60,
        elevation_gain_meters=None,
        elevation_loss_meters=None,
        min_altitude_meters=None,
        max_altitude_meters=None,
        max_road_angle_degrees=None,
        road_types=["path"],
        quality_status="future_status",
        quality_policy_version="v1",
        warnings=["warning"],
    )

    class OutputSession:
        async def get(self, _model: object, _id: object) -> object:
            return snapshot

        async def scalar(self, _statement: object) -> object:
            return '{"type":"LineString","coordinates":[[34,44],[34.1,44.1]]}'

    result = await routing_snapshot_out(OutputSession(), snapshot_id)
    assert isinstance(result, RouteExecutionRoutingOut)
    assert result.quality_status == "unknown"
    assert result.geometry is not None

    assert await routing_snapshot_out(OutputSession(), None) is None


def test_retention_cutoff_is_utc_and_bounds_operator_input() -> None:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    assert retention_cutoff(30, now=now) == now - timedelta(days=30)
    with pytest.raises(ValueError, match="retention_days"):
        retention_cutoff(0)
    with pytest.raises(ValueError, match="retention_days"):
        retention_cutoff(MAX_RETENTION_DAYS + 1)


def test_retention_query_is_bounded_and_mentions_reference_guard() -> None:
    statement = eligible_snapshot_ids(
        cutoff=datetime(2025, 1, 1, tzinfo=UTC),
        limit=25,
    )
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "route_routing_snapshots.created_at" in sql
    assert "route_executions" in sql
    assert "LIMIT 25" in sql
    with pytest.raises(ValueError, match="limit"):
        eligible_snapshot_ids(cutoff=datetime.now(UTC), limit=0)


class _RetentionScalars:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def all(self) -> list[object]:
        return self._values


class _RetentionResult:
    rowcount = 0


class _RetentionSession:
    def __init__(self, batches: list[list[object]]) -> None:
        self.batches = batches
        self.calls = 0
        self.executed = 0
        self.flushed = 0

    async def scalars(self, _statement: object) -> _RetentionScalars:
        values = self.batches[min(self.calls, len(self.batches) - 1)]
        self.calls += 1
        return _RetentionScalars(values)

    async def execute(self, _statement: object) -> _RetentionResult:
        self.executed += 1
        return _RetentionResult()

    async def flush(self) -> None:
        self.flushed += 1


@pytest.mark.asyncio
async def test_retention_deletes_in_bounded_batches_and_dry_run_is_read_only() -> None:
    first = _RetentionSession(batches=[[uuid4(), uuid4()], []])
    result = await purge_routing_snapshots(
        first,
        cutoff=datetime.now(UTC),
        batch_size=2,
    )
    assert result.scanned == 2
    assert result.deleted == 2
    assert result.batches == 2
    assert first.executed == 1
    assert first.flushed == 1

    dry = _RetentionSession(batches=[[uuid4()]])
    result = await purge_routing_snapshots(
        dry,
        cutoff=datetime.now(UTC),
        dry_run=True,
    )
    assert result.scanned == 1
    assert result.deleted == 0
    assert dry.executed == 0

"""Factory for DistanceMatrixProvider implementations (Workstream A)."""

from __future__ import annotations

from tourism_backend.config import Settings, get_settings
from tourism_backend.modules.route_builder.application.distance_matrix import (
    DistanceMatrixProvider,
)
from tourism_backend.modules.route_builder.infrastructure.distance_matrix_stub import (
    StubDistanceMatrixProvider,
)
from tourism_backend.modules.route_builder.infrastructure.two_gis_distance_matrix import (
    TwoGisDistanceMatrixProvider,
)


def get_distance_matrix_provider(settings: Settings | None = None) -> DistanceMatrixProvider:
    cfg = settings or get_settings()
    if cfg.distance_matrix_provider == "stub":
        return StubDistanceMatrixProvider()
    if cfg.distance_matrix_provider == "2gis":
        key = cfg.two_gis_http_api_key
        if key is None or not key.get_secret_value().strip():
            raise RuntimeError("TWO_GIS_HTTP_API_KEY is required for 2GIS distance matrix")
        return TwoGisDistanceMatrixProvider(
            api_key=key.get_secret_value(),
            base_url=cfg.two_gis_routing_base_url,
            timeout_seconds=cfg.routing_timeout_seconds,
            daily_call_budget=cfg.two_gis_distance_matrix_daily_call_budget,
        )
    raise RuntimeError(f"Unsupported distance_matrix_provider={cfg.distance_matrix_provider!r}")

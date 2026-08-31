"""Factory for TspProvider implementations (Workstream A)."""

from __future__ import annotations

from tourism_backend.config import Settings, get_settings
from tourism_backend.modules.route_builder.application.tsp import TspProvider
from tourism_backend.modules.route_builder.infrastructure.tsp_stub import (
    StubTspProvider,
)
from tourism_backend.modules.route_builder.infrastructure.two_gis_tsp import (
    TwoGisTspProvider,
)


def get_tsp_provider(settings: Settings | None = None) -> TspProvider:
    cfg = settings or get_settings()
    if cfg.tsp_provider == "stub":
        return StubTspProvider()
    if cfg.tsp_provider == "2gis":
        key = cfg.two_gis_http_api_key
        if key is None or not key.get_secret_value().strip():
            raise RuntimeError("TWO_GIS_HTTP_API_KEY is required for 2GIS TSP")
        return TwoGisTspProvider(
            api_key=key.get_secret_value(),
            base_url=cfg.two_gis_routing_base_url,
            timeout_seconds=cfg.routing_timeout_seconds,
            daily_call_budget=cfg.two_gis_tsp_daily_call_budget,
        )
    raise RuntimeError(f"Unsupported tsp_provider={cfg.tsp_provider!r}")

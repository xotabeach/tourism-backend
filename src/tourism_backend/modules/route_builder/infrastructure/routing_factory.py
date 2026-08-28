"""Factory for ADR-004 RoutingProvider implementations."""

from __future__ import annotations

from tourism_backend.config import Settings, get_settings
from tourism_backend.modules.route_builder.application.routing import RoutingProvider
from tourism_backend.modules.route_builder.infrastructure.routing_stub import (
    StubRoutingProvider,
)
from tourism_backend.modules.route_builder.infrastructure.two_gis_routing import (
    TwoGisRoutingProvider,
)


def get_routing_provider(settings: Settings | None = None) -> RoutingProvider:
    cfg = settings or get_settings()
    if cfg.routing_provider == "stub":
        return StubRoutingProvider()
    if cfg.routing_provider == "2gis":
        key = cfg.two_gis_http_api_key
        if key is None or not key.get_secret_value().strip():
            raise RuntimeError("TWO_GIS_HTTP_API_KEY is required for 2GIS routing")
        filters = tuple(
            item.strip() for item in cfg.two_gis_routing_filters.split(",") if item.strip()
        )
        return TwoGisRoutingProvider(
            api_key=key.get_secret_value(),
            base_url=cfg.two_gis_routing_base_url,
            timeout_seconds=cfg.routing_timeout_seconds,
            alternative=cfg.two_gis_routing_alternative,
            max_route_meters=cfg.two_gis_max_route_meters,
            filters=filters,
        )
    raise RuntimeError(f"Unsupported routing_provider={cfg.routing_provider!r}")

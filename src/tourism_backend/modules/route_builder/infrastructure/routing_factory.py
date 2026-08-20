"""Factory for ADR-004 RoutingProvider implementations."""

from __future__ import annotations

from tourism_backend.config import Settings, get_settings
from tourism_backend.modules.route_builder.application.routing import RoutingProvider
from tourism_backend.modules.route_builder.infrastructure.routing_stub import (
    StubRoutingProvider,
)


def get_routing_provider(settings: Settings | None = None) -> RoutingProvider:
    cfg = settings or get_settings()
    if cfg.routing_provider == "stub":
        return StubRoutingProvider()
    raise RuntimeError(f"Unsupported routing_provider={cfg.routing_provider!r}")

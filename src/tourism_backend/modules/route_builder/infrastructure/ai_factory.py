"""Factory for AIPlanningProvider implementations."""

from __future__ import annotations

from tourism_backend.config import Settings, get_settings
from tourism_backend.modules.route_builder.application.ai import AIPlanningProvider
from tourism_backend.modules.route_builder.infrastructure.ai_mock import (
    MockAIPlanningProvider,
)
from tourism_backend.modules.route_builder.infrastructure.lm_studio import (
    LMStudioProvider,
)


def get_ai_planning_provider(settings: Settings | None = None) -> AIPlanningProvider:
    cfg = settings or get_settings()
    if cfg.ai_provider.value == "mock":
        return MockAIPlanningProvider()
    if cfg.ai_provider.value == "lmstudio":
        if not cfg.lm_studio_base_url or not cfg.lm_studio_model:
            raise RuntimeError("LM Studio base URL and model are required")
        api_key = (
            cfg.lm_studio_api_key.get_secret_value() if cfg.lm_studio_api_key is not None else None
        )
        return LMStudioProvider(
            base_url=cfg.lm_studio_base_url,
            model=cfg.lm_studio_model,
            api_key=api_key,
            timeout_seconds=cfg.ai_request_timeout_seconds,
        )
    raise RuntimeError(f"Unsupported ai_provider={cfg.ai_provider.value!r}")

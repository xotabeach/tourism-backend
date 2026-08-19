#!/usr/bin/env python3
"""Check backend connectivity to a configured LM Studio server."""

from __future__ import annotations

import asyncio

from tourism_backend.config import get_settings
from tourism_backend.modules.route_builder.infrastructure.lm_studio import LMStudioProvider


async def _run() -> None:
    settings = get_settings()
    if not settings.lm_studio_base_url or not settings.lm_studio_model:
        raise SystemExit("Set LM_STUDIO_BASE_URL and LM_STUDIO_MODEL first")
    provider = LMStudioProvider(
        base_url=settings.lm_studio_base_url,
        model=settings.lm_studio_model,
        api_key=(
            settings.lm_studio_api_key.get_secret_value()
            if settings.lm_studio_api_key is not None
            else None
        ),
        timeout_seconds=settings.ai_request_timeout_seconds,
    )
    result = await provider.probe()
    print(
        "LM Studio probe OK: "
        f"provider={result.provider} model={result.configured_model} "
        f"available_models={len(result.available_models)}"
    )
    print(f"Response: {result.response_text}")


if __name__ == "__main__":
    asyncio.run(_run())

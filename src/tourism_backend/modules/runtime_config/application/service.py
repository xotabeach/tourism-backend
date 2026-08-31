"""Read/write for admin-editable runtime settings (Workstream B).

Deliberately NOT process-cached: the whole point is that an admin's change
in ``/admin`` takes effect on the next chat turn, not after a redeploy —
unlike ``config.get_settings()``, which is ``@lru_cache``d for the life of
the process (see the "переключение" gap called out in
``docs/ai-dual-provider-content-backlog-2026-08-31.md``, Workstream B).
Callers on a hot path should still keep this to one lookup per request, not
sprinkle it everywhere.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.config import AIProvider, Settings
from tourism_backend.modules.runtime_config.infrastructure.models import RuntimeSetting

AI_PROVIDER_KEY = "ai_provider"
_ALLOWED_AI_PROVIDER_OVERRIDES = frozenset(provider.value for provider in AIProvider)

_logger = logging.getLogger("tourism_backend.runtime_config")


async def get_runtime_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(RuntimeSetting, key)
    return row.value if row is not None else None


async def set_runtime_setting(
    session: AsyncSession,
    *,
    key: str,
    value: str,
    updated_by_principal_id: UUID | None,
) -> None:
    now = datetime.now(UTC)
    stmt = (
        insert(RuntimeSetting)
        .values(
            key=key,
            value=value,
            updated_at=now,
            updated_by_principal_id=updated_by_principal_id,
        )
        .on_conflict_do_update(
            index_elements=[RuntimeSetting.key],
            set_={
                "value": value,
                "updated_at": now,
                "updated_by_principal_id": updated_by_principal_id,
            },
        )
    )
    await session.execute(stmt)
    await session.commit()


async def effective_ai_provider_settings(session: AsyncSession, settings: Settings) -> Settings:
    """``settings`` with ``ai_provider`` overridden by the admin toggle, if set.

    Falls back to the static env default on any DB read failure or an
    unrecognized/blank stored value — a stale or broken override must never
    take the chat down, only give up quietly and use what the process was
    deployed with.
    """
    try:
        raw = await get_runtime_setting(session, AI_PROVIDER_KEY)
    except Exception:  # noqa: BLE001 — a DB hiccup must not break the chat turn
        _logger.warning("runtime_ai_provider_lookup_failed", exc_info=True)
        return settings
    if raw not in _ALLOWED_AI_PROVIDER_OVERRIDES:
        return settings
    return settings.model_copy(update={"ai_provider": AIProvider(raw)})

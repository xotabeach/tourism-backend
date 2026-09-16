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
from dataclasses import dataclass
from datetime import UTC, datetime
from string import Formatter
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.config import AIProvider, Settings
from tourism_backend.modules.runtime_config.application.company_details_schemas import (
    CompanyDetailsOut,
)
from tourism_backend.modules.runtime_config.infrastructure.models import (
    CompanyDetails,
    RuntimeSetting,
)

AI_PROVIDER_KEY = "ai_provider"
_ALLOWED_AI_PROVIDER_OVERRIDES = frozenset(provider.value for provider in AIProvider)
SMS_PROVIDER_KEY = "sms_provider"
SMS_TEMPLATE_KEY = "sms_otp_template"
SMS_SENDER_KEY = "sms_sender"
_ALLOWED_SMS_PROVIDER_OVERRIDES = frozenset({"stub", "smsaero"})

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
    commit: bool = True,
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
    if commit:
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


@dataclass(frozen=True)
class SmsRuntimeSettings:
    provider: str
    template: str
    sender: str


def validate_sms_template(value: str) -> str:
    value = value.strip()
    if not 2 <= len(value) <= 640:
        raise ValueError("Длина шаблона должна быть от 2 до 640 символов.")
    try:
        fields = [
            (field_name, format_spec, conversion)
            for _literal, field_name, format_spec, conversion in Formatter().parse(value)
            if field_name is not None
        ]
    except (KeyError, ValueError) as exc:
        raise ValueError("В шаблоне допустима только подстановка {code}.") from exc
    if fields != [("code", "", None)]:
        raise ValueError("Шаблон должен содержать только одну подстановку {code}.")
    return value


def validate_sms_sender(value: str) -> str:
    value = value.strip()
    if not 2 <= len(value) <= 64:
        raise ValueError("Длина имени отправителя должна быть от 2 до 64 символов.")
    return value


async def effective_sms_settings(session: AsyncSession, settings: Settings) -> SmsRuntimeSettings:
    """Resolve hot overrides; invalid values safely fall back to env defaults."""
    try:
        provider_raw = await get_runtime_setting(session, SMS_PROVIDER_KEY)
        template_raw = await get_runtime_setting(session, SMS_TEMPLATE_KEY)
        sender_raw = await get_runtime_setting(session, SMS_SENDER_KEY)
    except Exception:  # noqa: BLE001 — delivery falls back to validated env config
        _logger.warning("runtime_sms_config_lookup_failed", exc_info=True)
        provider_raw = template_raw = sender_raw = None

    provider = (
        provider_raw if provider_raw in _ALLOWED_SMS_PROVIDER_OVERRIDES else settings.sms_provider
    )
    try:
        template = validate_sms_template(template_raw or settings.sms_otp_template)
    except ValueError:
        template = settings.sms_otp_template
    try:
        sender = validate_sms_sender(sender_raw or settings.sms_sender)
    except ValueError:
        sender = settings.sms_sender
    return SmsRuntimeSettings(provider=provider, template=template, sender=sender)


COMPANY_DETAILS_ROW_ID = 1


async def get_company_details(session: AsyncSession) -> CompanyDetailsOut:
    """The singleton row, seeded by migration — always present."""
    row = await session.get(CompanyDetails, COMPANY_DETAILS_ROW_ID)
    if row is None:
        # Defensive only: the migration seeds row id=1, so this should never
        # trigger outside a hand-rolled test DB that skipped seeding.
        return CompanyDetailsOut(
            legal_name="",
            brand_name="КрымТрип",
            inn="",
            ogrn="",
            address="",
            email="",
            phone="",
            telegram="",
            working_hours="",
        )
    return CompanyDetailsOut(
        legal_name=row.legal_name,
        brand_name=row.brand_name,
        inn=row.inn,
        ogrn=row.ogrn,
        address=row.address,
        email=row.email,
        phone=row.phone,
        telegram=row.telegram,
        working_hours=row.working_hours,
    )

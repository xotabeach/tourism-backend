"""Typed, admin-editable anti-fraud thresholds on top of ``runtime_settings``.

``runtime_settings`` stores plain strings, so a typo (empty, text, zero,
negative) could silently switch detection off or block everyone. Every key
therefore has bounds: the admin form rejects an invalid value, and a bad
value that still reaches the table is replaced by the default on read with a
warning, never trusted.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.route_execution.application.antifraud_logic import (
    EscalationRules,
    GpsRules,
    PaceRules,
)
from tourism_backend.modules.runtime_config.infrastructure.models import RuntimeSetting

_logger = logging.getLogger("tourism_backend.antifraud")

KEY_PREFIX = "af_"


class AntiFraudMode(StrEnum):
    #: Nothing is recorded or enforced.
    OFF = "off"
    #: Violations and "would have" outcomes are recorded; nobody is affected.
    SHADOW = "shadow"
    #: Limits, flags, blocks and holds are applied.
    ENFORCE = "enforce"


@dataclass(frozen=True, slots=True)
class _IntSpec:
    default: int
    minimum: int
    maximum: int


@dataclass(frozen=True, slots=True)
class _FloatSpec:
    default: float
    minimum: float
    maximum: float


_INT_SPECS: dict[str, _IntSpec] = {
    "af_pace_leg_min_estimate_seconds": _IntSpec(120, 30, 600),
    "af_batch_window_seconds": _IntSpec(60, 10, 300),
    "af_flag_violations": _IntSpec(3, 2, 20),
    "af_flag_window_hours": _IntSpec(3, 1, 24),
    "af_block_violations": _IntSpec(6, 3, 50),
    "af_block_window_hours": _IntSpec(6, 1, 48),
    "af_ladder_memory_days": _IntSpec(30, 7, 90),
    "af_route_points_cooldown_days": _IntSpec(14, 0, 90),
    "af_daily_points_cap": _IntSpec(600, 100, 5000),
    "af_min_mark_gap_seconds": _IntSpec(10, 2, 60),
    "af_gps_tolerance_meters": _IntSpec(150, 50, 500),
    "af_gps_min_accuracy_meters": _IntSpec(100, 20, 300),
    "af_hold_overdue_days": _IntSpec(7, 1, 60),
}
_FLOAT_SPECS: dict[str, _FloatSpec] = {
    "af_pace_violation_ratio": _FloatSpec(0.5, 0.1, 0.9),
    "af_min_mark_ratio": _FloatSpec(0.2, 0.05, 0.5),
}
KEY_MODE = "af_mode"
# Which leg estimate judges pace: the straight line (as before OSM) or the
# router's own legs. Router legs start in observation only (spec 12a, D4).
KEY_PACE_SOURCE = "af_pace_source"
PACE_SOURCES: tuple[str, ...] = ("straight_line", "provider")
KEY_BLOCK_LADDER = "af_block_ladder_hours"

DEFAULT_LADDER: tuple[int, ...] = (1, 24, 168)
_LADDER_MAX_HOURS = 24 * 30
_LADDER_MAX_STEPS = 3

ALL_KEYS: frozenset[str] = frozenset(
    {KEY_MODE, KEY_PACE_SOURCE, KEY_BLOCK_LADDER, *_INT_SPECS.keys(), *_FLOAT_SPECS.keys()}
)


@dataclass(frozen=True, slots=True)
class AntiFraudSettings:
    mode: AntiFraudMode = AntiFraudMode.SHADOW
    pace: PaceRules = field(default_factory=PaceRules)
    gps: GpsRules = field(default_factory=GpsRules)
    escalation: EscalationRules = field(default_factory=EscalationRules)
    batch_window_seconds: int = 60
    route_points_cooldown_days: int = 14
    daily_points_cap: int = 600
    hold_overdue_days: int = 7
    pace_source: str = "straight_line"

    @property
    def enforcing(self) -> bool:
        return self.mode is AntiFraudMode.ENFORCE

    @property
    def recording(self) -> bool:
        return self.mode is not AntiFraudMode.OFF


def parse_ladder(value: str) -> tuple[int, ...]:
    """``"1,24,168"`` → ``(1, 24, 168)``; raises ``ValueError`` when invalid."""

    parts = [part.strip() for part in value.split(",") if part.strip()]
    if not 1 <= len(parts) <= _LADDER_MAX_STEPS:
        raise ValueError(f"Нужно от 1 до {_LADDER_MAX_STEPS} значений через запятую.")
    hours: list[int] = []
    for part in parts:
        if not part.isdigit():
            raise ValueError("Ступени лестницы — целые числа часов.")
        number = int(part)
        if not 1 <= number <= _LADDER_MAX_HOURS:
            raise ValueError(f"Каждая ступень — от 1 до {_LADDER_MAX_HOURS} часов.")
        hours.append(number)
    return tuple(hours)


def validate_setting(key: str, value: str) -> str:
    """Normalize an admin-entered value or raise ``ValueError`` (nothing is saved)."""

    text = value.strip()
    if key == KEY_MODE:
        try:
            return AntiFraudMode(text).value
        except ValueError as exc:
            raise ValueError("Режим: off, shadow или enforce.") from exc
    if key == KEY_PACE_SOURCE:
        if text not in PACE_SOURCES:
            raise ValueError("Источник оценки: straight_line или provider.")
        return text
    if key == KEY_BLOCK_LADDER:
        return ",".join(str(hours) for hours in parse_ladder(text))
    int_spec = _INT_SPECS.get(key)
    if int_spec is not None:
        if not text.lstrip("-").isdigit():
            raise ValueError("Нужно целое число.")
        number = int(text)
        if not int_spec.minimum <= number <= int_spec.maximum:
            raise ValueError(f"Допустимо от {int_spec.minimum} до {int_spec.maximum}.")
        return str(number)
    float_spec = _FLOAT_SPECS.get(key)
    if float_spec is not None:
        try:
            ratio = float(text.replace(",", "."))
        except ValueError as exc:
            raise ValueError("Нужно число, например 0.5.") from exc
        if not float_spec.minimum <= ratio <= float_spec.maximum:
            raise ValueError(f"Допустимо от {float_spec.minimum} до {float_spec.maximum}.")
        return str(ratio)
    raise ValueError("Неизвестная настройка.")


def _int_value(raw: Mapping[str, str], key: str) -> int:
    spec = _INT_SPECS[key]
    text = raw.get(key)
    if text is None:
        return spec.default
    try:
        return int(validate_setting(key, text))
    except ValueError:
        _logger.warning("antifraud_invalid_setting", extra={"key": key})
        return spec.default


def _float_value(raw: Mapping[str, str], key: str) -> float:
    spec = _FLOAT_SPECS[key]
    text = raw.get(key)
    if text is None:
        return spec.default
    try:
        return float(validate_setting(key, text))
    except ValueError:
        _logger.warning("antifraud_invalid_setting", extra={"key": key})
        return spec.default


def parse_settings(raw: Mapping[str, str]) -> AntiFraudSettings:
    """Build settings from raw stored values; every bad value falls back to its default."""

    mode = AntiFraudMode.SHADOW
    if (stored_mode := raw.get(KEY_MODE)) is not None:
        try:
            mode = AntiFraudMode(stored_mode.strip())
        except ValueError:
            _logger.warning("antifraud_invalid_setting", extra={"key": KEY_MODE})

    ladder = DEFAULT_LADDER
    if (stored_ladder := raw.get(KEY_BLOCK_LADDER)) is not None:
        try:
            ladder = parse_ladder(stored_ladder)
        except ValueError:
            _logger.warning("antifraud_invalid_setting", extra={"key": KEY_BLOCK_LADDER})

    pace_source = "straight_line"
    if (stored_source := raw.get(KEY_PACE_SOURCE)) is not None:
        if stored_source.strip() in PACE_SOURCES:
            pace_source = stored_source.strip()
        else:
            _logger.warning("antifraud_invalid_setting", extra={"key": KEY_PACE_SOURCE})

    flag_violations = _int_value(raw, "af_flag_violations")
    block_violations = max(_int_value(raw, "af_block_violations"), flag_violations)
    batch_window = _int_value(raw, "af_batch_window_seconds")
    return AntiFraudSettings(
        mode=mode,
        pace=PaceRules(
            ratio=_float_value(raw, "af_pace_violation_ratio"),
            min_estimate_seconds=_int_value(raw, "af_pace_leg_min_estimate_seconds"),
            min_mark_ratio=_float_value(raw, "af_min_mark_ratio"),
            min_mark_gap_seconds=_int_value(raw, "af_min_mark_gap_seconds"),
        ),
        gps=GpsRules(
            tolerance_meters=_int_value(raw, "af_gps_tolerance_meters"),
            min_accuracy_meters=_int_value(raw, "af_gps_min_accuracy_meters"),
        ),
        escalation=EscalationRules(
            flag_violations=flag_violations,
            flag_window_hours=_int_value(raw, "af_flag_window_hours"),
            block_violations=block_violations,
            block_window_hours=_int_value(raw, "af_block_window_hours"),
            block_ladder_hours=ladder,
            ladder_memory_days=_int_value(raw, "af_ladder_memory_days"),
        ),
        batch_window_seconds=batch_window,
        route_points_cooldown_days=_int_value(raw, "af_route_points_cooldown_days"),
        daily_points_cap=_int_value(raw, "af_daily_points_cap"),
        hold_overdue_days=_int_value(raw, "af_hold_overdue_days"),
        pace_source=pace_source,
    )


async def load_settings(session: AsyncSession) -> AntiFraudSettings:
    """Read all anti-fraud keys in one query; a DB hiccup falls back to defaults.

    Falling back to defaults means ``shadow``: detection keeps recording and
    nobody is punished because of a configuration read failure.
    """

    try:
        rows = (
            (
                await session.execute(
                    select(RuntimeSetting.key, RuntimeSetting.value).where(
                        RuntimeSetting.key.like(f"{KEY_PREFIX}%")
                    )
                )
            )
            .tuples()
            .all()
        )
    except Exception:  # noqa: BLE001 — never break a stop mark over a settings read
        _logger.warning("antifraud_settings_lookup_failed", exc_info=True)
        return AntiFraudSettings()
    return parse_settings(dict(rows))


@dataclass(frozen=True, slots=True)
class SettingDescription:
    """What the admin form needs to render one key."""

    key: str
    kind: str  # "mode" | "choice" | "ladder" | "int" | "float"
    default: str
    minimum: float | None = None
    maximum: float | None = None
    options: tuple[str, ...] = ()


def describe_settings() -> list[SettingDescription]:
    """Every editable key with its default and bounds, in display order."""

    described = [
        SettingDescription(KEY_MODE, "mode", AntiFraudMode.SHADOW.value),
        SettingDescription(KEY_PACE_SOURCE, "choice", "straight_line", options=PACE_SOURCES),
        SettingDescription(KEY_BLOCK_LADDER, "ladder", ",".join(map(str, DEFAULT_LADDER))),
    ]
    described += [
        SettingDescription(key, "float", str(spec.default), spec.minimum, spec.maximum)
        for key, spec in _FLOAT_SPECS.items()
    ]
    described += [
        SettingDescription(key, "int", str(spec.default), spec.minimum, spec.maximum)
        for key, spec in _INT_SPECS.items()
    ]
    return described

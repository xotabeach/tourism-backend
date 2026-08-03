"""Admin datetime display in Europe/Moscow (МСК)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from markupsafe import Markup, escape
from sqladmin.formatters import BASE_FORMATTERS

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

ADMIN_COLUMN_TYPE_FORMATTERS = {
    **BASE_FORMATTERS,
    datetime: lambda value: format_moscow_datetime(value),
    date: lambda value: format_moscow_date(value),
}


def to_moscow(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(MOSCOW_TZ)


def format_moscow_datetime(value: datetime) -> Markup:
    local = to_moscow(value)
    visible = local.strftime("%d.%m.%Y %H:%M")
    title = local.strftime("%d.%m.%Y %H:%M:%S") + " МСК"
    return Markup(
        '<span class="badge bg-secondary text-light my-1 py-1 px-2 '
        'lead d-inline-block text-truncate" title="{}">'
        '<i class="fa-solid fa-calendar-days"></i> {} МСК</span>'
    ).format(escape(title), escape(visible))


def format_moscow_date(value: date) -> Markup:
    visible = value.strftime("%d.%m.%Y")
    return Markup(
        '<span class="badge bg-secondary text-light my-1 py-1 px-2 '
        'lead d-inline-block text-truncate" title="{} МСК">'
        '<i class="fa-solid fa-calendar-days"></i> {}</span>'
    ).format(escape(visible), escape(visible))


def format_moscow_plain(value: datetime | None, *, with_seconds: bool = False) -> str:
    if value is None:
        return "—"
    local = to_moscow(value)
    if with_seconds:
        return local.strftime("%d.%m.%Y %H:%M:%S") + " МСК"
    return local.strftime("%d.%m.%Y %H:%M") + " МСК"

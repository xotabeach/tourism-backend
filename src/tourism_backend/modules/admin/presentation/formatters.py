"""Safe column formatters — allowlisted enums only, never raw user HTML."""

from __future__ import annotations

from markupsafe import Markup, escape

_STATUS_LABELS = {
    "open": ("Открыт", "ct-badge-open"),
    "closed": ("Закрыт", "ct-badge-closed"),
}

_KIND_LABELS = {
    "chat": ("Чат", "ct-badge-chat"),
    "route_error": ("Ошибка маршрута", "ct-badge-route_error"),
    "app_error": ("Ошибка приложения", "ct-badge-app_error"),
}

_AUTHOR_LABELS = {
    "user": ("Пользователь", "ct-badge-user"),
    "operator": ("Оператор", "ct-badge-operator"),
    "assistant": ("Ассистент", "ct-badge-assistant"),
    "system": ("Система", "ct-badge-system"),
}

_ROLE_LABELS = {
    "ops": ("Ops", "ct-badge-ops"),
    "admin": ("Admin", "ct-badge-admin"),
}

_ALLOWED_CSS = frozenset(
    {
        "ct-badge-open",
        "ct-badge-closed",
        "ct-badge-chat",
        "ct-badge-route_error",
        "ct-badge-app_error",
        "ct-badge-user",
        "ct-badge-operator",
        "ct-badge-assistant",
        "ct-badge-system",
        "ct-badge-ops",
        "ct-badge-admin",
    }
)


def _badge(value: object, mapping: dict[str, tuple[str, str]]) -> Markup:
    key = str(value or "")
    label, css = mapping.get(key, (key, "ct-badge-closed"))
    if css not in _ALLOWED_CSS:
        css = "ct-badge-closed"
    # css is allowlisted; label is escaped.
    return Markup('<span class="ct-badge {}">{}</span>').format(css, escape(label))


def format_ticket_status(model: object, attribute: object) -> Markup:
    return _badge(getattr(model, "status", None), _STATUS_LABELS)


def format_ticket_kind(model: object, attribute: object) -> Markup:
    return _badge(getattr(model, "kind", None), _KIND_LABELS)


def format_message_author(model: object, attribute: object) -> Markup:
    return _badge(getattr(model, "author", None), _AUTHOR_LABELS)


def format_admin_role(model: object, attribute: object) -> Markup:
    return _badge(getattr(model, "role", None), _ROLE_LABELS)


def format_debug_code(model: object, attribute: object) -> Markup:
    code = getattr(model, "debug_code", None)
    if not code:
        return Markup('<span class="text-secondary">—</span>')
    return Markup('<span class="ct-chip-mono">{}</span>').format(escape(str(code)))

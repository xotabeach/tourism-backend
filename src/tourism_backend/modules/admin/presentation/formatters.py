"""Safe column formatters — allowlisted enums only, never raw user HTML."""

from __future__ import annotations

from uuid import UUID

from markupsafe import Markup, escape
from starlette.requests import Request

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
        "ct-badge-awaiting",
        "ct-badge-answered",
    }
)


def _safe_media_url(url: object) -> str | None:
    """Only same-origin /media/ paths — never javascript: or external."""
    if not isinstance(url, str):
        return None
    if not url.startswith("/media/"):
        return None
    if ".." in url or "\\" in url or "\n" in url or "\r" in url:
        return None
    return url


def _user_media(request: Request | None, user_id: UUID) -> tuple[str | None, str | None]:
    cache = getattr(getattr(request, "state", None), "user_media", None)
    if not isinstance(cache, dict):
        return None, None
    entry = cache.get(user_id)
    if not isinstance(entry, dict):
        return None, None
    return _safe_media_url(entry.get("avatar")), _safe_media_url(entry.get("cover"))


def _badge(value: object, mapping: dict[str, tuple[str, str]]) -> Markup:
    key = str(value or "")
    label, css = mapping.get(key, (key, "ct-badge-closed"))
    if css not in _ALLOWED_CSS:
        css = "ct-badge-closed"
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


def format_ticket_awaiting(model: object, attribute: object) -> Markup:
    """Highlight tickets whose last human message is from the user."""
    status = str(getattr(model, "status", "") or "")
    last_human = str(getattr(model, "last_human_author", "") or "")
    if status != "closed" and last_human == "user":
        return Markup(
            '<span class="ct-badge ct-badge-awaiting ct-ticket-awaiting">Ждёт ответа</span>'
        )
    if last_human == "operator":
        return Markup('<span class="ct-badge ct-badge-answered">Отвечено</span>')
    return Markup('<span class="text-secondary">—</span>')


def format_user_id_peek(model: object, attribute: object) -> Markup:
    """UUID chip; hover/click loads name+phone via /admin/api/user-brief."""
    raw = getattr(model, "user_id", None)
    if raw is None:
        return Markup('<span class="text-secondary">—</span>')
    uid = str(raw)
    short = uid[:8]
    return Markup(
        '<button type="button" class="ct-user-peek" data-user-id="{}" '
        'title="Показать пользователя">{}…</button>'
    ).format(escape(uid), escape(short))


def format_user_avatar_name(
    model: object,
    attribute: object,
    request: Request | None = None,
) -> Markup:
    name = escape(str(getattr(model, "display_name", "") or "—"))
    user_id = getattr(model, "id", None)
    avatar = None
    if isinstance(user_id, UUID):
        avatar, _cover = _user_media(request, user_id)
    if avatar:
        thumb = Markup(
            '<img class="ct-user-avatar" src="{}" alt="" width="36" height="36" '
            'loading="lazy" decoding="async">'
        ).format(escape(avatar))
    else:
        initial = escape(str(getattr(model, "display_name", "?") or "?")[:1].upper())
        thumb = Markup('<span class="ct-user-avatar ct-user-avatar-fallback">{}</span>').format(
            initial
        )
    return Markup(
        '<span class="ct-user-profile-cell">{}<span class="ct-user-name">{}</span></span>'
    ).format(thumb, name)


def format_user_cover(
    model: object,
    attribute: object,
    request: Request | None = None,
) -> Markup:
    user_id = getattr(model, "id", None)
    if not isinstance(user_id, UUID):
        return Markup('<span class="text-secondary">—</span>')
    _avatar, cover = _user_media(request, user_id)
    uid = str(user_id)
    short = escape(uid[:8])
    id_chip = Markup(
        '<button type="button" class="ct-user-peek" data-user-id="{}" title="{}">{}…</button>'
    ).format(escape(uid), escape(uid), short)
    if cover:
        banner = Markup(
            '<span class="ct-user-banner"><img src="{}" alt="" '
            'loading="lazy" decoding="async"></span>'
        ).format(escape(cover))
    else:
        banner = Markup('<span class="ct-user-banner ct-user-banner-empty">нет баннера</span>')
    return Markup('<span class="ct-user-banner-cell">{}{}</span>').format(banner, id_chip)

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

_ROUTE_STATUS_LABELS = {
    "draft": ("Черновик", "ct-badge-route-draft"),
    "pending_review": ("На модерации", "ct-badge-route-pending"),
    "published": ("Опубликован", "ct-badge-route-published"),
    "rejected": ("Нужны правки", "ct-badge-route-rejected"),
    "deleted": ("Удалён", "ct-badge-route-deleted"),
}

_REVIEW_STATUS_LABELS = {
    "pending_review": ("На модерации", "ct-badge-route-pending"),
    "published": ("Опубликован", "ct-badge-route-published"),
    "rejected": ("Отклонён", "ct-badge-route-rejected"),
    "deleted": ("Удалён", "ct-badge-route-deleted"),
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
        "ct-badge-route-draft",
        "ct-badge-route-pending",
        "ct-badge-route-published",
        "ct-badge-route-rejected",
        "ct-badge-route-deleted",
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


def format_route_publication_status(model: object, attribute: object) -> Markup:
    return _badge(getattr(model, "publication_status", None), _ROUTE_STATUS_LABELS)


def format_review_status(model: object, attribute: object) -> Markup:
    return _badge(getattr(model, "status", None), _REVIEW_STATUS_LABELS)


def _entity_cache(request: Request | None, key: str) -> dict[UUID, str]:
    cache = getattr(getattr(request, "state", None), key, None)
    if isinstance(cache, dict):
        return cache
    return {}


def _admin_details_href(
    request: Request | None,
    *,
    identity: str,
    pk: str,
) -> str:
    if request is None:
        return f"/admin/{identity}/details/{pk}"
    try:
        return str(request.url_for("admin:details", identity=identity, pk=pk))
    except Exception:  # noqa: BLE001
        return f"/admin/{identity}/details/{pk}"


def format_user_fk(
    model: object,
    attribute: object,
    request: Request | None = None,
) -> Markup:
    """Show display name + link; UUID only as secondary tooltip chip."""
    attr_name = getattr(attribute, "key", None) or getattr(attribute, "name", None)
    raw = None
    if isinstance(attr_name, str):
        raw = getattr(model, attr_name, None)
    if raw is None:
        raw = getattr(model, "user_id", None)
    if raw is None:
        raw = getattr(model, "owner_user_id", None)
    if raw is None:
        raw = getattr(model, "author_user_id", None)
    if raw is None:
        return Markup('<span class="text-secondary">—</span>')
    uid = UUID(str(raw)) if not isinstance(raw, UUID) else raw
    names = _entity_cache(request, "user_names")
    label = escape(names.get(uid) or "Пользователь")
    href = escape(_admin_details_href(request, identity="user", pk=str(uid)))
    short = escape(str(uid)[:8])
    return Markup(
        '<span class="ct-entity-ref">'
        '<a class="ct-entity-link" href="{}">{}</a> '
        '<button type="button" class="ct-user-peek ct-id-soft" data-user-id="{}" '
        'title="{}">{}…</button>'
        "</span>"
    ).format(href, label, escape(str(uid)), escape(str(uid)), short)


def format_route_fk(
    model: object,
    attribute: object,
    request: Request | None = None,
) -> Markup:
    raw = getattr(model, "route_id", None)
    if raw is None:
        return Markup('<span class="text-secondary">—</span>')
    rid = UUID(str(raw)) if not isinstance(raw, UUID) else raw
    names = _entity_cache(request, "route_names")
    label = escape(names.get(rid) or "Маршрут")
    href = escape(_admin_details_href(request, identity="route", pk=str(rid)))
    short = escape(str(rid)[:8])
    return Markup(
        '<span class="ct-entity-ref">'
        '<a class="ct-entity-link" href="{}">{}</a> '
        '<span class="ct-id-soft" title="{}">{}…</span>'
        "</span>"
    ).format(href, label, escape(str(rid)), short)


def format_review_body_preview(model: object, attribute: object) -> Markup:
    body = str(getattr(model, "body", "") or "")
    preview = body if len(body) <= 80 else body[:79] + "…"
    return Markup('<span class="ct-review-body" title="{}">{}</span>').format(
        escape(body),
        escape(preview),
    )


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


def format_user_id_peek(
    model: object,
    attribute: object,
    request: Request | None = None,
) -> Markup:
    """Name + details link when cached; falls back to UUID peek chip."""
    raw = getattr(model, "user_id", None)
    if raw is None:
        return Markup('<span class="text-secondary">—</span>')
    return format_user_fk(model, attribute, request)


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

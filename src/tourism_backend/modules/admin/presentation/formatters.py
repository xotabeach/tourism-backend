"""Safe column formatters — allowlisted enums only, never raw user HTML."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from markupsafe import Markup, escape
from starlette.requests import Request

from tourism_backend.modules.admin.presentation.datetime_fmt import format_moscow_plain

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

_PLACE_STATUS_LABELS = {
    "draft": ("Черновик", "ct-badge-route-draft"),
    "published": ("Опубликовано", "ct-badge-route-published"),
    "rejected": ("Отклонено", "ct-badge-route-rejected"),
    "archived": ("В архиве", "ct-badge-route-deleted"),
}

_REVIEW_STATUS_LABELS = {
    "pending_review": ("На модерации", "ct-badge-route-pending"),
    "published": ("Опубликован", "ct-badge-route-published"),
    "rejected": ("Отклонён", "ct-badge-route-rejected"),
    "deleted": ("Удалён", "ct-badge-route-deleted"),
}

_HELP_STATUS_LABELS = {
    "draft": ("Черновик", "ct-badge-route-draft"),
    "published": ("Опубликована", "ct-badge-route-published"),
    "withdrawn": ("Отозвана", "ct-badge-route-deleted"),
}

_ARTICLE_STATUS_LABELS = {
    "draft": ("Черновик", "ct-badge-route-draft"),
    "pending_review": ("На модерации", "ct-badge-route-pending"),
    "published": ("Опубликована", "ct-badge-route-published"),
    "rejected": ("Отклонена", "ct-badge-route-rejected"),
    "deleted": ("Удалена", "ct-badge-route-deleted"),
}

# Жалобы: статус разбора и причина, выбранная человеком в приложении.
_REPORT_STATUS_LABELS = {
    "new": ("Новая", "ct-badge-route-pending"),
    "in_review": ("В работе", "ct-badge-awaiting"),
    "resolved": ("Решена", "ct-badge-route-published"),
    "rejected": ("Отклонена", "ct-badge-route-rejected"),
}

_REPORT_REASON_LABELS = {
    "spam": ("Спам или реклама", "ct-badge-closed"),
    "abuse": ("Оскорбления", "ct-badge-closed"),
    "inappropriate": ("Неприемлемое содержимое", "ct-badge-closed"),
    "misinformation": ("Недостоверные сведения", "ct-badge-closed"),
    "copyright": ("Чужие материалы", "ct-badge-closed"),
    "other": ("Другое", "ct-badge-closed"),
}

_REPORT_TARGET_LABELS = {
    "article_comment": ("Комментарий", "ct-badge-chat"),
    "article": ("Статья", "ct-badge-chat"),
    "route": ("Маршрут", "ct-badge-chat"),
    "place": ("Место", "ct-badge-chat"),
    "companion_group": ("Приглашение", "ct-badge-chat"),
}

_SMS_DELIVERY_STATUS_LABELS = {
    "pending": ("Ожидает", "ct-badge-route-pending"),
    "sending": ("Отправляется", "ct-badge-awaiting"),
    "sent": ("Принято шлюзом", "ct-badge-route-published"),
    "failed": ("Ошибка", "ct-badge-route-rejected"),
}

_HOLD_STATUS_LABELS = {
    "held": ("На проверке", "ct-badge-route-pending"),
    "approved": ("Подтверждено", "ct-badge-route-published"),
    "rejected": ("Отклонено", "ct-badge-route-rejected"),
}

_POINTS_STATUS_LABELS = {
    "none": ("—", "ct-badge-closed"),
    "awarded": ("Начислены", "ct-badge-route-published"),
    "held": ("На проверке", "ct-badge-route-pending"),
    "rejected": ("Отклонены", "ct-badge-route-rejected"),
}

_VIOLATION_KIND_LABELS = {
    "too_fast": ("Слишком быстро", "ct-badge-route-pending"),
    "ahead": ("Отметка впереди", "ct-badge-route-rejected"),
}

_VIOLATION_MODE_LABELS = {
    "shadow": ("Наблюдение", "ct-badge-closed"),
    "enforce": ("Боевой", "ct-badge-route-rejected"),
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


def format_help_status(model: object, attribute: object) -> Markup:
    return _badge(getattr(model, "status", None), _HELP_STATUS_LABELS)


def format_help_review_until(model: object, attribute: object) -> Markup:
    """The deadline, plus how close it is.

    An expired article vanishes from search without any other signal, so the
    list has to say so before an operator goes looking for why the help is
    empty. Renewal is deliberate: nothing extends this on its own.
    """
    value = getattr(model, "review_until", None)
    if not isinstance(value, datetime):
        return Markup('<span class="ct-badge ct-badge-closed">—</span>')
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    stamp = escape(format_moscow_plain(moment, with_seconds=False))
    if getattr(model, "status", None) != "published":
        return Markup("<span>{}</span>").format(stamp)
    days = (moment - now).days
    if moment <= now:
        return Markup('{} <span class="ct-badge ct-badge-route-rejected">истёк</span>').format(
            stamp
        )
    if days <= 14:
        return Markup(
            '{} <span class="ct-badge ct-badge-route-pending">осталось {} дн.</span>'
        ).format(stamp, days)
    return Markup('{} <span class="ct-badge ct-badge-route-published">до {} дн.</span>').format(
        stamp, days
    )


def format_route_publication_status(model: object, attribute: object) -> Markup:
    return _badge(getattr(model, "publication_status", None), _ROUTE_STATUS_LABELS)


def format_place_publication_status(model: object, attribute: object) -> Markup:
    return _badge(getattr(model, "publication_status", None), _PLACE_STATUS_LABELS)


def format_review_status(model: object, attribute: object) -> Markup:
    return _badge(getattr(model, "status", None), _REVIEW_STATUS_LABELS)


def format_report_status(model: object, attribute: object) -> Markup:
    return _badge(getattr(model, "status", None), _REPORT_STATUS_LABELS)


def format_sms_delivery_status(model: object, attribute: object) -> Markup:
    return _badge(getattr(model, "status", None), _SMS_DELIVERY_STATUS_LABELS)


def format_report_reason(model: object, attribute: object) -> Markup:
    return _badge(getattr(model, "reason", None), _REPORT_REASON_LABELS)


def format_report_target_type(model: object, attribute: object) -> Markup:
    return _badge(getattr(model, "target_type", None), _REPORT_TARGET_LABELS)


def format_article_status(model: object, attribute: object) -> Markup:
    """Articles carry one status reviews do not: an author-only draft."""
    return _badge(getattr(model, "status", None), _ARTICLE_STATUS_LABELS)


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


def format_place_fk(
    model: object,
    attribute: object,
    request: Request | None = None,
) -> Markup:
    raw = getattr(model, "place_id", None)
    if raw is None:
        return Markup('<span class="text-secondary">—</span>')
    place_id = UUID(str(raw)) if not isinstance(raw, UUID) else raw
    names = _entity_cache(request, "place_names")
    label = escape(names.get(place_id) or "Локация")
    href = escape(_admin_details_href(request, identity="place", pk=str(place_id)))
    short = escape(str(place_id)[:8])
    return Markup(
        '<span class="ct-entity-ref">'
        '<a class="ct-entity-link" href="{}">{}</a> '
        '<span class="ct-id-soft" title="{}">{}…</span>'
        "</span>"
    ).format(href, label, escape(str(place_id)), short)


def format_review_body_preview(model: object, attribute: object) -> Markup:
    body = str(getattr(model, "body", "") or "")
    preview = body if len(body) <= 80 else body[:79] + "…"
    return Markup('<span class="ct-review-body" title="{}">{}</span>').format(
        escape(body),
        escape(preview),
    )


def format_review_media_gallery(
    model: object,
    attribute: object,
    request: Request | None = None,
) -> Markup:
    review_id = getattr(model, "id", None)
    cache = getattr(getattr(request, "state", None), "review_media", None)
    urls = cache.get(review_id, []) if isinstance(cache, dict) else []
    safe_urls = [safe for raw in urls if (safe := _safe_media_url(raw)) is not None]
    if not safe_urls:
        return Markup('<span class="text-secondary">Нет фото</span>')
    buttons = Markup("").join(
        Markup(
            '<button type="button" class="ct-review-media-thumb" '
            'data-ct-review-image="{}" aria-label="Открыть фото отзыва">'
            '<img src="{}" alt="" loading="lazy" decoding="async"></button>'
        ).format(escape(url), escape(url))
        for url in safe_urls
    )
    return Markup('<span class="ct-review-media-grid">{}</span>').format(buttons)


def format_article_block_image(
    model: object,
    attribute: object,
    request: Request | None = None,
) -> Markup:
    """Thumbnail for an image block, resolved through `request.state`.

    Without this the admin showed a bare media-attachment UUID, so a moderator
    deciding whether to publish an article could not see the pictures in it
    (reported 2026-09-03).
    """
    attachment_id = getattr(model, "media_attachment_id", None)
    if attachment_id is None:
        return Markup('<span class="text-secondary">—</span>')
    cache = getattr(getattr(request, "state", None), "article_block_media", None)
    raw = cache.get(attachment_id) if isinstance(cache, dict) else None
    url = _safe_media_url(raw)
    if url is None:
        return Markup('<span class="text-secondary">Нет фото</span>')
    return Markup(
        '<span class="ct-review-media-grid">'
        '<button type="button" class="ct-review-media-thumb" '
        'data-ct-review-image="{}" aria-label="Открыть изображение блока">'
        '<img src="{}" alt="" loading="lazy" decoding="async"></button></span>'
    ).format(escape(url), escape(url))


def format_expert_status(model: object, attribute: object) -> Markup:
    if bool(getattr(model, "is_expert", False)):
        return Markup('<span class="ct-badge ct-badge-route-published">Эксперт</span>')
    return Markup('<span class="text-secondary">Нет</span>')


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


def format_masked_token(model: object, attribute: object) -> Markup:
    """Show only enough of a push token to match a device.

    A device token is a credential: printing it in full would let anyone with
    admin read access send pushes to that device.
    """
    token = getattr(model, "token", None)
    if not isinstance(token, str) or not token:
        return Markup("—")
    tail = token[-6:] if len(token) > 6 else token
    return Markup("<code>…{}</code>").format(tail)


def format_hold_status(model: object, attribute: object) -> Markup:
    return _badge(getattr(model, "status", None), _HOLD_STATUS_LABELS)


def format_points_status(model: object, attribute: object) -> Markup:
    return _badge(getattr(model, "points_status", None), _POINTS_STATUS_LABELS)


def format_violation_kind(model: object, attribute: object) -> Markup:
    return _badge(getattr(model, "kind", None), _VIOLATION_KIND_LABELS)


def format_violation_mode(model: object, attribute: object) -> Markup:
    return _badge(getattr(model, "mode", None), _VIOLATION_MODE_LABELS)


def format_fraud_flag(model: object, attribute: object) -> Markup:
    """Flagged / trusted as a badge; blocked-until is a plain timestamp column."""
    key = getattr(attribute, "key", None) or getattr(attribute, "name", None)
    if not isinstance(key, str):
        return Markup("")
    if bool(getattr(model, key, False)):
        label, css = (
            ("Доверенный", "ct-badge-route-published")
            if key == "is_trusted"
            else ("Отмечен", "ct-badge-route-rejected")
        )
        return Markup('<span class="ct-badge {}">{}</span>').format(css, label)
    return Markup('<span class="text-secondary">—</span>')


def choice_formatter(field: str, labels: dict[str, str]) -> Callable[[object, object], Markup]:
    """Translate a stored value without changing it; escape future unknown values."""

    def render(model: object, attribute: object) -> Markup:
        value = getattr(model, field, None)
        if value is None or value == "":
            return escape("—")
        key = str(value)
        return escape(labels.get(key, key))

    return render


def format_route_revision_link(
    model: object,
    attribute: object,
    request: Request | None = None,
) -> Markup:
    """«Сравнить версии» on an edit of a published route (spec 15, D6)."""
    if getattr(model, "revision_of_route_id", None) is None:
        return Markup("")
    return Markup('<a href="/admin/route-revision?route_id={}">Сравнить версии</a>').format(
        getattr(model, "id", "")
    )


def format_route_name_with_structure(
    model: object,
    attribute: object,
    request: Request | None = None,
) -> Markup:
    """Route name with a link to its days and car parks page (spec 14)."""
    name = getattr(model, "name", "") or ""
    route_id = getattr(model, "id", None)
    return Markup('{} · <a href="/admin/route-structure?route_id={}">Дни и парковки</a>').format(
        name, route_id
    )


_CHECK_TRANSPORT = {"walk": "пешком", "car": "на машине"}


def _check_rows(metrics: dict[str, object]) -> list[tuple[str, str]]:
    def figure(key: str, unit: str) -> str:
        value = metrics.get(key)
        return "нет данных" if value is None else f"{value} {unit}"

    kinds = metrics.get("road_types")
    road_types = kinds if isinstance(kinds, list) else []
    distance = figure("distance_km", "км")
    if metrics.get("distance_estimated"):
        distance += " (оценка по прямой)"
    return [
        ("Как", str(_CHECK_TRANSPORT.get(str(metrics.get("transport")), "нет данных"))),
        ("Длина", distance),
        ("Время в движении", figure("movement_hours", "ч")),
        ("Набор высоты", figure("elevation_gain_meters", "м")),
        ("Дней", figure("day_count", "")),
        ("Сложность", figure("difficulty_level", "из 5")),
        ("Линия", "по дорогам и тропам" if metrics.get("line_drawn") else "прямая"),
        ("Самый длинный перегон по прямой", figure("longest_gap_km", "км")),
        ("Разброс точек", figure("spread_km", "км")),
        ("Типы дорог", ", ".join(str(kind) for kind in road_types) or "—"),
    ]


def format_route_checks(
    model: object,
    attribute: object,
    request: Request | None = None,
) -> Markup:
    """Figures and warnings of the last submission check (BACKEND-41).

    The moderator sees length, gain, time and what the router warned about
    instead of a raw JSON blob; the blob stays below, folded.
    """
    access = getattr(model, "accessibility", None)
    access = access if isinstance(access, dict) else {}
    check = access.get("submit_check")
    raw = Markup("<details><summary>Все данные</summary><pre>{}</pre></details>").format(
        json.dumps(access, ensure_ascii=False, indent=2, default=str)
    )
    if not isinstance(check, dict):
        routing = access.get("routing")
        if not isinstance(routing, dict):
            return Markup("<p>Проверки при отправке не было</p>") + raw
        meters = routing.get("distance_meters")
        seconds = routing.get("movement_duration_seconds")
        metrics: dict[str, object] = {
            "distance_km": round(meters / 1000, 1) if isinstance(meters, int | float) else None,
            "movement_hours": round(seconds / 3600, 1)
            if isinstance(seconds, int | float)
            else None,
            "elevation_gain_meters": routing.get("elevation_gain_meters"),
            "line_drawn": routing.get("synthetic") is False,
            "road_types": routing.get("road_types") or [],
            "transport": "car" if routing.get("transport_mode") == "car" else "walk",
            "difficulty_level": getattr(model, "difficulty_level", None),
        }
        head = Markup("<p>Проверки при отправке не было, расчёты маршрутизатора:</p>")
        problems = Markup("")
    else:
        stored = check.get("metrics")
        metrics = stored if isinstance(stored, dict) else {}
        checked = str(check.get("checked_at") or "")
        with contextlib.suppress(ValueError):
            checked = format_moscow_plain(datetime.fromisoformat(checked))
        verdict = (
            Markup('<b style="color:#1a7f37">прошёл</b>')
            if check.get("passed")
            else Markup('<b style="color:#cf222e">не прошёл</b>')
        )
        head = Markup("<p>Проверка при отправке {}: {}</p>").format(checked, verdict)
        problems = Markup("")
        for key, colour, title in (
            ("blockers", "#cf222e", "Не пускает на модерацию"),
            ("warnings", "#9a6700", "Обратить внимание"),
        ):
            items = [item for item in check.get(key) or [] if isinstance(item, dict)]
            if not items:
                continue
            problems += Markup('<p style="color:{}"><b>{}</b></p><ul>').format(colour, title)
            for item in items:
                problems += Markup("<li>{}</li>").format(item.get("message") or item.get("code"))
            problems += Markup("</ul>")
    table = Markup('<table class="table table-sm" style="width:auto">')
    for label, value in _check_rows(metrics):
        table += Markup("<tr><th>{}</th><td>{}</td></tr>").format(label, value)
    table += Markup("</table>")
    return head + problems + table + raw

"""Why a moderator returned a route to its author (spec 15, D6).

A rejection always carries a reason: the author gets it in the notification
and sees it next to the route, so they know what to fix.
"""

from __future__ import annotations

from dataclasses import dataclass

NOTE_MAX_LENGTH = 500


@dataclass(frozen=True)
class RejectionReason:
    code: str
    #: Label of the moderator's action in the admin panel.
    admin_label: str
    #: What the author reads.
    author_text: str
    #: The moderator's own note is the whole explanation.
    needs_note: bool = False


REASONS: tuple[RejectionReason, ...] = (
    RejectionReason(
        "photos",
        "Вернуть: фотографии",
        "Нужны другие фотографии: обложка и фото точек должны показывать сам маршрут",
    ),
    RejectionReason(
        "stops",
        "Вернуть: точки и порядок",
        "Проверьте точки и их порядок: маршрут не складывается в понятный путь",
    ),
    RejectionReason(
        "description",
        "Вернуть: описание",
        "Дополните описание: путешественнику должно быть понятно, что его ждёт",
    ),
    RejectionReason(
        "duplicate",
        "Вернуть: такой маршрут уже есть",
        "Такой маршрут уже есть в каталоге",
    ),
    RejectionReason(
        "publish_as_new",
        "Вернуть: опубликуйте как новый",
        "Изменений слишком много: опубликуйте их как новый маршрут",
    ),
    RejectionReason(
        "rules",
        "Вернуть: нарушает правила",
        "Маршрут нарушает правила публикации",
    ),
    RejectionReason(
        "other",
        "Вернуть: по заметке модератора",
        "Модератор оставил замечание",
        needs_note=True,
    ),
)

_BY_CODE = {reason.code: reason for reason in REASONS}


def reason_by_code(code: str | None) -> RejectionReason | None:
    return _BY_CODE.get(code) if code else None


def clean_note(note: str | None) -> str | None:
    """The moderator's note without stray whitespace, or ``None`` if empty."""

    text = " ".join((note or "").split())
    return text[:NOTE_MAX_LENGTH] or None


def rejection_text(code: str | None, note: str | None) -> str | None:
    """What the author is told, or ``None`` when the route was not rejected.

    A rejection stored before reasons existed has neither a code nor a note
    and gets no text: the app then shows only that the route was returned.
    """

    reason = reason_by_code(code)
    cleaned = clean_note(note)
    if reason is None:
        return cleaned
    if cleaned is None:
        return reason.author_text
    if reason.needs_note:
        return cleaned
    return f"{reason.author_text}. {cleaned}"

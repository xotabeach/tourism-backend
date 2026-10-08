"""BACKEND-34: a returned route always tells its author what to fix."""

from types import SimpleNamespace

import pytest

from tourism_backend.modules.routes.application.rejection import (
    NOTE_MAX_LENGTH,
    REASONS,
    clean_note,
    reason_by_code,
    rejection_text,
)
from tourism_backend.modules.routes.application.service import author_rejection_text


def test_every_reason_has_a_code_a_label_and_a_text_for_the_author() -> None:
    codes = [reason.code for reason in REASONS]
    assert len(codes) == len(set(codes))
    for reason in REASONS:
        assert len(reason.code) <= 32
        assert reason.admin_label.startswith("Вернуть: ")
        assert reason.author_text
        assert not reason.author_text.endswith(".")
    assert [reason.code for reason in REASONS if reason.needs_note] == ["other"]


def test_reason_alone_is_its_own_text() -> None:
    assert rejection_text("photos", None) == reason_by_code("photos").author_text  # type: ignore[union-attr]
    assert rejection_text("photos", "   ") == reason_by_code("photos").author_text  # type: ignore[union-attr]


def test_note_is_added_after_the_reason() -> None:
    text = rejection_text("stops", "  Третья точка\nстоит  не на тропе ")
    assert text == (
        "Проверьте точки и их порядок: маршрут не складывается в понятный путь. "
        "Третья точка стоит не на тропе"
    )


def test_other_reason_is_only_the_note() -> None:
    assert rejection_text("other", "Нет фото финиша") == "Нет фото финиша"
    # Reaching the author without a note would say nothing; the admin action
    # refuses that case, this is the fallback wording if it ever happens.
    assert rejection_text("other", None) == "Модератор оставил замечание"


def test_a_rejection_from_before_reasons_existed_has_no_text() -> None:
    assert rejection_text(None, None) is None
    assert rejection_text("no-such-code", None) is None
    assert rejection_text(None, "Старая заметка") == "Старая заметка"


def test_note_is_trimmed_to_the_column_length() -> None:
    assert clean_note("") is None
    assert clean_note(None) is None
    assert len(clean_note("я" * (NOTE_MAX_LENGTH + 50)) or "") == NOTE_MAX_LENGTH


@pytest.mark.parametrize(
    ("status", "shown"),
    [
        ("rejected", True),
        # The author edited the returned route: it is a draft again and the
        # note still tells them what to fix until they resend it.
        ("draft", True),
        ("pending_review", False),
        ("published", False),
        ("deleted", False),
    ],
)
def test_reason_is_shown_only_while_the_author_has_to_resend(status: str, shown: bool) -> None:
    route = SimpleNamespace(
        publication_status=status,
        rejection_reason="description",
        moderator_note=None,
    )
    text = author_rejection_text(route)  # type: ignore[arg-type]
    assert (text is not None) is shown


class _Session:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, item: object) -> None:
        self.added.append(item)


async def test_notification_carries_the_reason() -> None:
    from uuid import uuid4

    from tourism_backend.modules.notifications.application import service

    session = _Session()
    with_reason = await service.create_route_moderation_notification(
        session,  # type: ignore[arg-type]
        owner_user_id=uuid4(),
        route_id=uuid4(),
        route_name="Тропа Голицына",
        approved=False,
        reason="Нужны другие фотографии",
    )
    without_reason = await service.create_route_moderation_notification(
        session,  # type: ignore[arg-type]
        owner_user_id=uuid4(),
        route_id=uuid4(),
        route_name="Тропа Голицына",
        approved=False,
    )

    assert with_reason.kind == "route_rejected"
    assert with_reason.body.endswith("вернули на доработку. Нужны другие фотографии")
    assert "Исправьте замечания" in without_reason.body
    assert len(session.added) == 2


async def test_a_long_reason_still_fits_the_notification() -> None:
    from uuid import uuid4

    from tourism_backend.modules.notifications.application import service

    notification = await service.create_route_moderation_notification(
        _Session(),  # type: ignore[arg-type]
        owner_user_id=uuid4(),
        route_id=uuid4(),
        route_name="Маршрут",
        approved=False,
        reason="я" * 600,
    )

    assert len(notification.body) <= 500

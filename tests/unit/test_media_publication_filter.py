"""M-2: cover/reusable media must not leak unpublished catalog photos."""

from uuid import uuid4

from sqlalchemy.dialects import postgresql

from tourism_backend.modules.media.application.service import reusable_covers_stmt
from tourism_backend.modules.places.application.place_covers import (
    generic_published_attachment_cover_stmt,
    published_place_attachment_covers_stmt,
)


def test_published_place_cover_stmt_filters_publication_status() -> None:
    compiled = str(
        published_place_attachment_covers_stmt([uuid4()]).compile(dialect=postgresql.dialect())
    ).lower()
    assert "places.publication_status" in compiled


def test_generic_fallback_cover_stmt_filters_publication_status() -> None:
    compiled = str(
        generic_published_attachment_cover_stmt().compile(dialect=postgresql.dialect())
    ).lower()
    assert "places.publication_status" in compiled
    assert "limit" in compiled


def test_reusable_covers_stmt_requires_published_parent() -> None:
    compiled = str(reusable_covers_stmt(limit=10).compile(dialect=postgresql.dialect())).lower()
    assert "places.publication_status" in compiled
    assert "routes.publication_status" in compiled
    assert "exists" in compiled

"""M-4: concurrent refresh must lock the session row before rotation."""

from sqlalchemy.dialects import postgresql

from tourism_backend.modules.identity.application.service import refresh_session_lock_stmt


def test_refresh_session_lock_stmt_uses_for_update() -> None:
    compiled = str(refresh_session_lock_stmt("abc").compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE" in compiled
    assert "AUTH_REFRESH_SESSIONS" in compiled

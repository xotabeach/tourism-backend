"""AI-5: generation quota must lock the user row before counting."""

from uuid import uuid4

from sqlalchemy.dialects import postgresql

from tourism_backend.modules.route_builder.application.quota import user_quota_lock_stmt


def test_user_quota_lock_stmt_uses_for_update() -> None:
    compiled = str(user_quota_lock_stmt(uuid4()).compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE" in compiled
    assert "USERS" in compiled

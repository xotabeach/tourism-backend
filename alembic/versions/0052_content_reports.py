"""Жалобы на контент.

Один список на всё, что можно опубликовать: комментарий, статью, маршрут,
место. Отдельные таблицы на каждый тип развалились бы в админке — модератору
нужен один поток «что разбирать», а не пять.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052_content_reports"
down_revision: str | Sequence[str] | None = "0051_article_views"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TARGET_TYPES = "'article_comment', 'article', 'route', 'place'"
_REASONS = "'spam', 'abuse', 'inappropriate', 'misinformation', 'copyright', 'other'"
_STATUSES = "'new', 'in_review', 'resolved', 'rejected'"


def upgrade() -> None:
    op.create_table(
        "content_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("reporter_user_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("comment", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="new"),
        sa.Column("resolution_note", sa.String(length=500), nullable=True),
        sa.Column("resolved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["reporter_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resolved_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_content_reports"),
        sa.CheckConstraint(f"target_type IN ({_TARGET_TYPES})", name="ck_content_reports_target"),
        sa.CheckConstraint(f"reason IN ({_REASONS})", name="ck_content_reports_reason"),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name="ck_content_reports_status"),
        # Один человек — одна жалоба на объект: повторные нажатия не должны
        # раздувать очередь модерации.
        sa.UniqueConstraint(
            "target_type",
            "target_id",
            "reporter_user_id",
            name="uq_content_reports_target_reporter",
        ),
    )
    op.create_index(
        "ix_content_reports_status_created",
        "content_reports",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_content_reports_target",
        "content_reports",
        ["target_type", "target_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_content_reports_target", table_name="content_reports")
    op.drop_index("ix_content_reports_status_created", table_name="content_reports")
    op.drop_table("content_reports")

"""Add independently moderated reviews for places."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_place_reviews"
down_revision: str | Sequence[str] | None = "0025_place_import_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_media_attachments_entity_type"),
        "media_attachments",
        type_="check",
    )
    op.create_check_constraint(
        "entity_type",
        "media_attachments",
        "entity_type IN ('user', 'place', 'route', 'review', 'place_review')",
    )

    op.create_table(
        "place_reviews",
        sa.Column("place_id", sa.Uuid(), nullable=False),
        sa.Column("author_user_id", sa.Uuid(), nullable=False),
        sa.Column("reply_to_review_id", sa.Uuid(), nullable=True),
        sa.Column("body", sa.String(length=2000), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("moderator_note", sa.String(length=500), nullable=True),
        sa.Column("moderated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rating >= 1 AND rating <= 5", name="ck_place_reviews_rating_range"),
        sa.CheckConstraint(
            "status IN ('pending_review', 'published', 'rejected', 'deleted')",
            name="ck_place_reviews_status",
        ),
        sa.ForeignKeyConstraint(["place_id"], ["places.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["reply_to_review_id"],
            ["place_reviews.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_place_reviews_place_id", "place_reviews", ["place_id"])
    op.create_index("ix_place_reviews_author_user_id", "place_reviews", ["author_user_id"])
    op.create_index(
        "ix_place_reviews_reply_to_review_id",
        "place_reviews",
        ["reply_to_review_id"],
    )
    op.create_index(
        "ix_place_reviews_place_status_created",
        "place_reviews",
        ["place_id", "status", "created_at"],
    )
    op.create_index(
        "ix_place_reviews_moderation_queue",
        "place_reviews",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_place_reviews_place_author_status",
        "place_reviews",
        ["place_id", "author_user_id", "status"],
    )


def downgrade() -> None:
    op.execute("DELETE FROM media_attachments WHERE entity_type = 'place_review'")
    op.drop_constraint(
        op.f("ck_media_attachments_entity_type"),
        "media_attachments",
        type_="check",
    )
    op.create_check_constraint(
        "entity_type",
        "media_attachments",
        "entity_type IN ('user', 'place', 'route', 'review')",
    )
    op.drop_table("place_reviews")

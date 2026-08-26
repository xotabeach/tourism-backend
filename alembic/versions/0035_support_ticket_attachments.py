"""Allow support tickets to hold gallery photo attachments.

Reuses the existing `media_attachments` table (entity_type/role scoping)
instead of a new table, the same way route/place reviews already do —
support-ticket photos are just another gallery under a new entity_type.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0035_support_ticket_attachments"
down_revision: str | Sequence[str] | None = "0034_place_merge_dedup"
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
        "entity_type IN ('user', 'place', 'route', 'review', 'place_review', 'support_ticket')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM media_attachments WHERE entity_type = 'support_ticket'")
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

"""Versions of a published route: an edit waits beside it (BACKEND-38, spec 15 D5, D6, D17)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0084_route_revisions"
down_revision: str | Sequence[str] | None = "0083_run_counted_share"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The edit of a published route is a route row of its own, pointing at
    # the route it will replace once a moderator approves it.
    op.add_column(
        "routes",
        sa.Column(
            "revision_of_route_id",
            sa.Uuid(),
            sa.ForeignKey("routes.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "uq_routes_one_revision",
        "routes",
        ["revision_of_route_id"],
        unique=True,
        postgresql_where=sa.text(
            "revision_of_route_id IS NOT NULL AND publication_status <> 'deleted'"
        ),
    )
    # When an approved edit last replaced the route's content: reviews older
    # than this were written about the previous version.
    op.add_column(
        "routes",
        sa.Column("content_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # A file copied into an edit keeps the id it had on the published route,
    # so an editor that still holds the old id keeps the right file.
    op.add_column("media_attachments", sa.Column("copied_from_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("media_attachments", "copied_from_id")
    op.drop_column("routes", "content_updated_at")
    # Edits that never replaced their route go with the feature.
    op.execute("DELETE FROM routes WHERE revision_of_route_id IS NOT NULL")
    op.drop_index("uq_routes_one_revision", table_name="routes")
    op.drop_column("routes", "revision_of_route_id")

"""Route generation proposals and usage counters."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_route_builder_generations"
down_revision: str | Sequence[str] | None = "0027_travel_plus_subscriptions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "route_proposals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("assistant_text", sa.Text(), nullable=False),
        sa.Column("params", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column(
            "place_ids",
            sa.dialects.postgresql.ARRAY(sa.Uuid()),
            nullable=False,
        ),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("cover_url", sa.Text(), nullable=True),
        sa.Column("route_id", sa.Uuid(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'accepted', 'rejected', 'superseded')",
            name="ck_route_proposals_status",
        ),
        sa.CheckConstraint(
            "channel IN ('form', 'chat')",
            name="ck_route_proposals_channel",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["route_id"], ["routes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_route_proposals_user_id", "route_proposals", ["user_id"])
    op.create_index("ix_route_proposals_status", "route_proposals", ["status"])
    op.create_index("ix_route_proposals_route_id", "route_proposals", ["route_id"])
    op.create_index(
        "ix_route_proposals_user_status",
        "route_proposals",
        ["user_id", "status"],
    )

    op.create_table(
        "route_generation_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=True),
        sa.Column("route_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "channel IN ('form', 'chat')",
            name="ck_route_generation_events_channel",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["route_proposals.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["route_id"], ["routes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_route_generation_events_user_id",
        "route_generation_events",
        ["user_id"],
    )
    op.create_index(
        "ix_route_generation_events_proposal_id",
        "route_generation_events",
        ["proposal_id"],
    )
    op.create_index(
        "ix_route_generation_events_route_id",
        "route_generation_events",
        ["route_id"],
    )
    op.create_index(
        "ix_route_generation_events_user_created",
        "route_generation_events",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_route_generation_events_user_created",
        table_name="route_generation_events",
    )
    op.drop_index(
        "ix_route_generation_events_route_id",
        table_name="route_generation_events",
    )
    op.drop_index(
        "ix_route_generation_events_proposal_id",
        table_name="route_generation_events",
    )
    op.drop_index(
        "ix_route_generation_events_user_id",
        table_name="route_generation_events",
    )
    op.drop_table("route_generation_events")
    op.drop_index("ix_route_proposals_user_status", table_name="route_proposals")
    op.drop_index("ix_route_proposals_route_id", table_name="route_proposals")
    op.drop_index("ix_route_proposals_status", table_name="route_proposals")
    op.drop_index("ix_route_proposals_user_id", table_name="route_proposals")
    op.drop_table("route_proposals")

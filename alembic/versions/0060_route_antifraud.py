"""Anti-fraud for route completion: leg estimates, violations, holds, fraud state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0060_route_antifraud"
down_revision: str | Sequence[str] | None = "0059_sms_delivery_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KINDS = (
    "kind IN ("
    "'route_review', 'route_published', 'route_rejected', "
    "'review_published', 'review_rejected', 'profile_like', "
    "'achievement_unlocked', 'support_reply', 'review_reply', "
    "'expert_granted', 'expert_revoked', "
    "'article_published', 'article_rejected', "
    "'article_comment', 'article_about_your_route'"
    ")"
)
_NEW_KINDS = (
    "kind IN ("
    "'route_review', 'route_published', 'route_rejected', "
    "'review_published', 'review_rejected', 'profile_like', "
    "'achievement_unlocked', 'support_reply', 'review_reply', "
    "'expert_granted', 'expert_revoked', "
    "'article_published', 'article_rejected', "
    "'article_comment', 'article_about_your_route', "
    "'antifraud_flagged', 'antifraud_blocked', 'antifraud_points_decision'"
    ")"
)
_ANTIFRAUD_KINDS = "'antifraud_flagged', 'antifraud_blocked', 'antifraud_points_decision'"


def upgrade() -> None:
    # ---- route_executions: what the run earned and why it may be limited
    op.add_column(
        "route_executions",
        sa.Column("computed_points", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "route_executions",
        sa.Column("points_status", sa.String(length=16), nullable=False, server_default="none"),
    )
    op.add_column(
        "route_executions",
        sa.Column("points_reason", sa.String(length=32), nullable=True),
    )
    # Existing runs were credited in full: keep their history truthful.
    op.execute(
        sa.text(
            "UPDATE route_executions SET computed_points = awarded_points, "
            "points_status = CASE WHEN awarded_points > 0 THEN 'awarded' ELSE 'none' END"
        )
    )
    op.create_check_constraint(
        "points_status",
        "route_executions",
        "points_status IN ('none', 'awarded', 'held', 'rejected')",
    )
    op.create_check_constraint(
        "computed_points_non_negative",
        "route_executions",
        "computed_points >= 0",
    )

    # ---- route_execution_stops: expected leg, computed when a run starts
    op.add_column(
        "route_execution_stops",
        sa.Column("leg_distance_meters", sa.Integer(), nullable=True),
    )
    op.add_column(
        "route_execution_stops",
        sa.Column("leg_estimate_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "route_execution_stops",
        sa.Column("leg_estimate_source", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "route_execution_stops",
        sa.Column(
            "mark_below_floor",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # ---- one row per suspicious stop mark (retained 90 days)
    op.create_table(
        "route_pace_violations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("stop_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("estimate_seconds", sa.Integer(), nullable=True),
        sa.Column("actual_seconds", sa.Integer(), nullable=True),
        sa.Column("gps_verdict", sa.String(length=8), nullable=True),
        sa.Column("gps_distance_bucket_m", sa.Integer(), nullable=True),
        sa.Column("timing_source", sa.String(length=8), nullable=False),
        sa.Column("offline_sync", sa.Boolean(), nullable=False),
        sa.Column("counted", sa.Boolean(), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('too_fast', 'ahead')",
            name="kind",
        ),
        sa.CheckConstraint(
            "mode IN ('shadow', 'enforce')",
            name="mode",
        ),
        sa.CheckConstraint(
            "timing_source IN ('server', 'device')",
            name="timing_source",
        ),
        sa.CheckConstraint(
            "gps_verdict IS NULL OR gps_verdict IN ('at', 'behind', 'ahead', 'unknown')",
            name="gps_verdict",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["execution_id"], ["route_executions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["stop_id"], ["route_execution_stops.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_route_pace_violations"),
    )
    op.create_index(
        "ix_route_pace_violations_user_occurred",
        "route_pace_violations",
        ["user_id", "occurred_at"],
    )
    op.create_index(
        "ix_route_pace_violations_execution_id",
        "route_pace_violations",
        ["execution_id"],
    )

    # ---- per-user state; a missing row means "normal"
    op.create_table(
        "user_fraud_state",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("is_flagged", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("flagged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ladder_level", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_offence_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("counters_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_trusted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", name="pk_user_fraud_state"),
    )

    # ---- points waiting for an operator
    op.create_table(
        "route_points_holds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("deducted_points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "status IN ('held', 'approved', 'rejected')",
            name="status",
        ),
        sa.CheckConstraint(
            "reason IN ('flag_retro', 'flag_forward')",
            name="reason",
        ),
        sa.CheckConstraint("amount >= 0", name="amount_non_negative"),
        sa.CheckConstraint(
            "deducted_points >= 0",
            name="deducted_non_negative",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["execution_id"], ["route_executions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decided_by"], ["admin_principals.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_route_points_holds"),
        sa.UniqueConstraint("execution_id", name="uq_route_points_holds_execution_id"),
    )
    op.create_index("ix_route_points_holds_user_id", "route_points_holds", ["user_id"])
    op.create_index(
        "ix_route_points_holds_status_created",
        "route_points_holds",
        ["status", "created_at"],
    )

    # ---- new in-app notification kinds
    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint("kind", "notifications", _NEW_KINDS)


def downgrade() -> None:
    # The old CHECK cannot hold the new kinds, so those rows go first.
    # _ANTIFRAUD_KINDS is a module-level literal, not user input.
    op.execute(
        sa.text(  # nosemgrep: avoid-sqlalchemy-text
            f"DELETE FROM notifications WHERE kind IN ({_ANTIFRAUD_KINDS})"
        )
    )
    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint("kind", "notifications", _OLD_KINDS)

    op.drop_index("ix_route_points_holds_status_created", table_name="route_points_holds")
    op.drop_index("ix_route_points_holds_user_id", table_name="route_points_holds")
    op.drop_table("route_points_holds")
    op.drop_table("user_fraud_state")
    op.drop_index("ix_route_pace_violations_execution_id", table_name="route_pace_violations")
    op.drop_index("ix_route_pace_violations_user_occurred", table_name="route_pace_violations")
    op.drop_table("route_pace_violations")

    op.drop_column("route_execution_stops", "mark_below_floor")
    op.drop_column("route_execution_stops", "leg_estimate_source")
    op.drop_column("route_execution_stops", "leg_estimate_seconds")
    op.drop_column("route_execution_stops", "leg_distance_meters")

    op.drop_constraint("computed_points_non_negative", "route_executions", type_="check")
    op.drop_constraint("points_status", "route_executions", type_="check")
    op.drop_column("route_executions", "points_reason")
    op.drop_column("route_executions", "points_status")
    op.drop_column("route_executions", "computed_points")

"""Persist immutable routing revisions and link them to route executions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography
from sqlalchemy.dialects import postgresql

revision: str = "0039_route_routing_snapshots"
down_revision: str | Sequence[str] | None = "0038_route_executions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "route_routing_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("provider_version", sa.String(length=64), nullable=True),
        sa.Column("transport_mode", sa.String(length=32), nullable=True),
        sa.Column(
            "geometry",
            Geography(geometry_type="LINESTRING", srid=4326),
            nullable=True,
        ),
        sa.Column("distance_meters", sa.Integer(), nullable=True),
        sa.Column("movement_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("visit_duration_minutes", sa.Integer(), nullable=True),
        sa.Column("transfer_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("buffer_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("total_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("elevation_gain_meters", sa.Integer(), nullable=True),
        sa.Column("elevation_loss_meters", sa.Integer(), nullable=True),
        sa.Column("min_altitude_meters", sa.Integer(), nullable=True),
        sa.Column("max_altitude_meters", sa.Integer(), nullable=True),
        sa.Column("max_road_angle_degrees", sa.Float(), nullable=True),
        sa.Column("road_types", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column(
            "quality_status",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("quality_policy_version", sa.String(length=32), nullable=True),
        sa.Column("warnings", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column(
            "requested_filters",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("route_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_route_routing_snapshots_revision_positive"),
        sa.CheckConstraint(
            "quality_status IN "
            "('unknown', 'unverified', 'checking', 'verified', "
            "'verified_with_warnings', 'needs_review', 'unusable')",
            name="ck_route_routing_snapshots_quality_status_allowed",
        ),
        sa.CheckConstraint(
            "distance_meters IS NULL OR distance_meters >= 0",
            name="ck_route_routing_snapshots_distance_non_negative",
        ),
        sa.CheckConstraint(
            "movement_duration_seconds IS NULL OR movement_duration_seconds >= 0",
            name="ck_route_routing_snapshots_movement_duration_non_negative",
        ),
        sa.CheckConstraint(
            "total_duration_seconds IS NULL OR total_duration_seconds >= 0",
            name="ck_route_routing_snapshots_total_duration_non_negative",
        ),
        sa.CheckConstraint(
            "max_road_angle_degrees IS NULL OR "
            "(max_road_angle_degrees >= 0 AND max_road_angle_degrees <= 90)",
            name="ck_route_routing_snapshots_road_angle_range",
        ),
        sa.ForeignKeyConstraint(["route_id"], ["routes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "route_id",
            "revision",
            name="uq_route_routing_snapshots_route_revision",
        ),
    )
    op.create_index(
        "ix_route_routing_snapshots_route_id",
        "route_routing_snapshots",
        ["route_id"],
    )
    op.create_index(
        "ix_route_routing_snapshots_route_captured",
        "route_routing_snapshots",
        ["route_id", "captured_at"],
    )
    op.add_column(
        "route_executions",
        sa.Column("routing_snapshot_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_route_executions_routing_snapshot_id_route_routing_snapshots",
        "route_executions",
        "route_routing_snapshots",
        ["routing_snapshot_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_route_executions_routing_snapshot_id",
        "route_executions",
        ["routing_snapshot_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_route_executions_routing_snapshot_id",
        table_name="route_executions",
    )
    op.drop_constraint(
        "fk_route_executions_routing_snapshot_id_route_routing_snapshots",
        "route_executions",
        type_="foreignkey",
    )
    op.drop_column("route_executions", "routing_snapshot_id")
    op.drop_index(
        "ix_route_routing_snapshots_route_captured",
        table_name="route_routing_snapshots",
    )
    op.drop_index(
        "ix_route_routing_snapshots_route_id",
        table_name="route_routing_snapshots",
    )
    op.drop_table("route_routing_snapshots")

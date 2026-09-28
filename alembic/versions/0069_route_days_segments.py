"""Days and segments of a route and of its routing snapshots (BACKEND-26, spec 14 step 0)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography

revision: str = "0069_route_days_segments"
down_revision: str | Sequence[str] | None = "0068_pace_estimate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FALSE = sa.text("false")


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.add_column(
        "routes",
        sa.Column("base_mode", sa.String(length=16), nullable=False, server_default="walk"),
    )
    op.add_column(
        "routes",
        sa.Column("needs_public_transport", sa.Boolean(), nullable=False, server_default=_FALSE),
    )
    op.add_column(
        "routes", sa.Column("days_manual", sa.Boolean(), nullable=False, server_default=_FALSE)
    )
    op.add_column(
        "routes", sa.Column("has_hard_day", sa.Boolean(), nullable=False, server_default=_FALSE)
    )
    # The five spellings found in routes.transport_mode (spec 14, D18).
    op.execute(
        """
        UPDATE routes SET base_mode = CASE
            WHEN lower(trim(transport_mode)) IN ('car', 'driving', 'drive', 'auto') THEN 'car'
            WHEN lower(trim(transport_mode)) IN
                ('mixed', 'public', 'public_transport', 'transit', 'bus') THEN 'mixed'
            ELSE 'walk'
        END,
        needs_public_transport = lower(trim(coalesce(transport_mode, ''))) IN
            ('public', 'public_transport', 'transit', 'bus')
        """
    )
    op.create_check_constraint("base_mode", "routes", "base_mode IN ('walk', 'car', 'mixed')")

    op.add_column(
        "route_stops",
        sa.Column("time_of_day", sa.String(length=8), nullable=False, server_default="any"),
    )
    op.create_check_constraint(
        "time_of_day", "route_stops", "time_of_day IN ('any', 'dark', 'dawn')"
    )

    op.create_table(
        "route_days",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("day_index", sa.Integer(), nullable=False),
        sa.Column("first_stop_id", sa.Uuid(), nullable=False),
        sa.Column("last_stop_id", sa.Uuid(), nullable=False),
        sa.Column("boundary_source", sa.String(length=8), nullable=False, server_default="auto"),
        sa.Column("overnight_note", sa.String(length=255), nullable=True),
        sa.Column("overloaded", sa.Boolean(), nullable=False, server_default=_FALSE),
        sa.Column("difficulty", sa.String(length=32), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("day_index >= 1", name="ck_route_days_day_index_positive"),
        sa.CheckConstraint(
            "boundary_source IN ('auto', 'manual')", name="ck_route_days_boundary_source"
        ),
        sa.ForeignKeyConstraint(
            ["route_id"], ["routes.id"], name="fk_route_days_route_id_routes", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["first_stop_id"],
            ["route_stops.id"],
            name="fk_route_days_first_stop_id_route_stops",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["last_stop_id"],
            ["route_stops.id"],
            name="fk_route_days_last_stop_id_route_stops",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_route_days"),
        sa.UniqueConstraint("route_id", "day_index", name="uq_route_days_route_day"),
    )
    op.create_index("ix_route_days_route_id", "route_days", ["route_id"])

    op.create_table(
        "route_segments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("leg_index", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("from_stop_id", sa.Uuid(), nullable=False),
        sa.Column("to_stop_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("distance_meters", sa.Integer(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("elevation_gain_meters", sa.Integer(), nullable=True),
        sa.Column("elevation_loss_meters", sa.Integer(), nullable=True),
        sa.Column("geometry", Geography(geometry_type="LINESTRING", srid=4326), nullable=True),
        sa.Column("quality_status", sa.String(length=32), nullable=True),
        sa.Column("transit_line_ref", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "leg_index >= 0 AND seq >= 0", name="ck_route_segments_order_non_negative"
        ),
        sa.CheckConstraint(
            "mode IN ('walk', 'car', 'bus', 'trolleybus', 'train', 'cable_car', 'ferry')",
            name="ck_route_segments_mode",
        ),
        sa.CheckConstraint("role IN ('main', 'approach', 'return')", name="ck_route_segments_role"),
        sa.CheckConstraint(
            "origin IN ('router', 'synthetic', 'editor', 'transit')",
            name="ck_route_segments_origin",
        ),
        sa.ForeignKeyConstraint(
            ["route_id"],
            ["routes.id"],
            name="fk_route_segments_route_id_routes",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["from_stop_id"],
            ["route_stops.id"],
            name="fk_route_segments_from_stop_id_route_stops",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_stop_id"],
            ["route_stops.id"],
            name="fk_route_segments_to_stop_id_route_stops",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_route_segments"),
        sa.UniqueConstraint("route_id", "leg_index", "seq", name="uq_route_segments_leg_seq"),
    )
    op.create_index("ix_route_segments_route_id", "route_segments", ["route_id"])

    # Adding nullable columns does not fire the snapshot UPDATE trigger;
    # old snapshots keep NULL and fall back to the route (spec 14, R1).
    op.add_column(
        "route_routing_snapshots",
        sa.Column("difficulty", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "route_routing_snapshots",
        sa.Column("base_mode", sa.String(length=16), nullable=True),
    )

    op.create_table(
        "routing_snapshot_days",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("day_index", sa.Integer(), nullable=False),
        sa.Column("first_position", sa.Integer(), nullable=False),
        sa.Column("last_position", sa.Integer(), nullable=False),
        sa.Column("boundary_source", sa.String(length=8), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["route_routing_snapshots.id"],
            name="fk_routing_snapshot_days_snapshot",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_routing_snapshot_days"),
        sa.UniqueConstraint("snapshot_id", "day_index", name="uq_routing_snapshot_days_day"),
    )
    op.create_index(
        "ix_routing_snapshot_days_snapshot_id", "routing_snapshot_days", ["snapshot_id"]
    )
    op.create_table(
        "routing_snapshot_segments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("leg_index", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("distance_meters", sa.Integer(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("elevation_gain_meters", sa.Integer(), nullable=True),
        sa.Column("elevation_loss_meters", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["route_routing_snapshots.id"],
            name="fk_routing_snapshot_segments_snapshot",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_routing_snapshot_segments"),
        sa.UniqueConstraint(
            "snapshot_id", "leg_index", "seq", name="uq_routing_snapshot_segments_leg_seq"
        ),
    )
    op.create_index(
        "ix_routing_snapshot_segments_snapshot_id",
        "routing_snapshot_segments",
        ["snapshot_id"],
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_routing_snapshot_part_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME;
        END;
        $$
        """
    )
    for table in ("routing_snapshot_days", "routing_snapshot_segments"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_immutable
            BEFORE UPDATE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION prevent_routing_snapshot_part_mutation()
            """
        )


def downgrade() -> None:
    for table in ("routing_snapshot_segments", "routing_snapshot_days"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS prevent_routing_snapshot_part_mutation()")
    op.drop_column("route_routing_snapshots", "base_mode")
    op.drop_column("route_routing_snapshots", "difficulty")
    op.drop_table("route_segments")
    op.drop_table("route_days")
    op.drop_constraint("time_of_day", "route_stops", type_="check")
    op.drop_column("route_stops", "time_of_day")
    op.drop_constraint("base_mode", "routes", type_="check")
    for column in ("has_hard_day", "days_manual", "needs_public_transport", "base_mode"):
        op.drop_column("routes", column)

"""Prevent mutation of routing facts after an execution snapshot is created."""

from collections.abc import Sequence

from alembic import op

revision: str = "0040_snapshot_immutable"
down_revision: str | Sequence[str] | None = "0039_route_routing_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``route_id`` is intentionally excluded from the comparison: the parent
    # route uses ON DELETE SET NULL, and losing that optional back-reference
    # must not make it impossible to remove a route. Every routing fact,
    # fingerprint and timestamp remains immutable.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_route_routing_snapshot_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF (to_jsonb(NEW) - 'route_id') IS DISTINCT FROM
               (to_jsonb(OLD) - 'route_id') THEN
                RAISE EXCEPTION 'route_routing_snapshots are immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER route_routing_snapshots_immutable
        BEFORE UPDATE ON route_routing_snapshots
        FOR EACH ROW
        EXECUTE FUNCTION prevent_route_routing_snapshot_mutation()
        """
    )
    op.execute(
        """
        COMMENT ON TABLE route_routing_snapshots IS
        'Append-only routing facts captured for route execution'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS route_routing_snapshots_immutable
        ON route_routing_snapshots;
        """
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_route_routing_snapshot_mutation();")

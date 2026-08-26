"""Give "Эксперт" a real seat in the travel_ranks catalog.

Previously `users.is_expert` was a bare boolean, entirely disconnected from
the travel_ranks ladder — an expert's displayed rank_title/rank_slug still
came from their travel_points, same as everyone else, and only the
leaderboard query had a bolted-on `is_expert = false` filter keeping them
out. That's an easy place for a future query to forget the filter.

This adds "Эксперт" as an actual (admin-only, unreachable-by-points) rank
row, backfills it onto every currently-expert user, and application code
now resolves an expert's rank to this row directly instead of computing one
from points — see `_resolve_rank` in public_service.py and the is_expert
guard in travel_points.py's `_sync_rank`.
"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "0037_expert_travel_rank"
down_revision: str | Sequence[str] | None = "0036_user_travel_preferences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXPERT_RANK_ID = UUID("00000000-0000-0000-0000-000000000106")
# Far above anything travel_points can reach — belt-and-suspenders on top of
# the application-layer guards that skip auto rank-sync for experts.
EXPERT_MIN_POINTS = 1_000_000_000


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO travel_ranks (id, slug, title, min_points, next_rank_points, sort_order) "
            "VALUES (:id, 'expert', 'Эксперт', :min_points, 0, 6)"
        ).bindparams(id=EXPERT_RANK_ID, min_points=EXPERT_MIN_POINTS)
    )
    op.execute(
        sa.text("UPDATE users SET rank_id = :expert_id WHERE is_expert = true").bindparams(
            expert_id=EXPERT_RANK_ID
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE users u SET rank_id = ("
            "SELECT tr.id FROM travel_ranks tr "
            "WHERE tr.min_points <= u.travel_points AND tr.id != :expert_id "
            "ORDER BY tr.min_points DESC LIMIT 1"
            ") WHERE u.rank_id = :expert_id"
        ).bindparams(expert_id=EXPERT_RANK_ID)
    )
    op.execute(sa.text("DELETE FROM travel_ranks WHERE id = :id").bindparams(id=EXPERT_RANK_ID))

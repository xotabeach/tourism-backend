"""Support for merging seed/OSM duplicate places (ADR-009 P0-bis 0b.1).

`scripts/dedupe_places.py` archives an OSM-imported duplicate into the seed
place it was matched to, instead of deleting it (`routes.place_id` is
`ondelete="RESTRICT"`, so a merged-away place must keep existing). This adds
the trail column; name-similarity scoring runs in Python (`difflib`), not
`pg_trgm` — the local dev Postgres image fails to load that extension
(`pg_trgm.so: undefined symbol: pg_mblen_unbounded`), and the dataset here
is small enough that scoring client-side costs nothing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_place_merge_dedup"
down_revision: str | Sequence[str] | None = "0033_place_osm_promoted_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "places",
        sa.Column("merged_into_place_id", sa.UUID(), nullable=True),
    )
    op.create_index(
        "ix_places_merged_into_place_id",
        "places",
        ["merged_into_place_id"],
    )
    op.create_foreign_key(
        "fk_places_merged_into_place_id",
        "places",
        "places",
        ["merged_into_place_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_places_merged_into_not_self",
        "places",
        "merged_into_place_id IS NULL OR merged_into_place_id != id",
    )


def downgrade() -> None:
    op.drop_constraint("ck_places_merged_into_not_self", "places", type_="check")
    op.drop_constraint("fk_places_merged_into_place_id", "places", type_="foreignkey")
    op.drop_index("ix_places_merged_into_place_id", table_name="places")
    op.drop_column("places", "merged_into_place_id")

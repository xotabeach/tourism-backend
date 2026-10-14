"""Where a place's price came from and when an editor checked it (BACKEND-55).

Spec 33, D6 and D15: only a price an editor confirmed counts for the route's
cost. A place gets its price's source (``editor`` or ``import``), the time it
was checked, and ``price_hint``: what OSM said, kept for the editor instead
of written into the price itself. Prices already in the table are not
erased: they become hints, and their payment status stays as an unconfirmed
one from the import.

Also a category flag «платное впечатление» (D11) and an optional price of a
route segment set by an editor (D14).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0093_price_provenance"
down_revision: str | Sequence[str] | None = "0092_author_points"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("places", sa.Column("price_source", sa.String(16), nullable=True))
    op.add_column(
        "places", sa.Column("price_checked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "places", sa.Column("price_hint", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.create_check_constraint("price_source", "places", "price_source IN ('editor', 'import')")
    op.create_check_constraint(
        "price_amounts",
        "places",
        "(price_min_amount IS NULL OR price_min_amount >= 0) AND "
        "(price_max_amount IS NULL OR price_min_amount IS NULL "
        "OR price_max_amount >= price_min_amount)",
    )
    # Nothing an import wrote was checked by anyone: amounts move to the hint,
    # a known payment status stays and is marked as the import's.
    op.execute(
        """
        UPDATE places SET
            price_hint = jsonb_strip_nulls(jsonb_build_object(
                'source', 'osm',
                'min', price_min_amount,
                'max', price_max_amount,
                'currency', price_currency,
                'notes', price_notes
            )),
            price_min_amount = NULL,
            price_max_amount = NULL,
            price_notes = NULL
        WHERE price_min_amount IS NOT NULL OR price_max_amount IS NOT NULL
            OR price_notes IS NOT NULL
        """
    )
    op.execute("UPDATE places SET price_source = 'import' WHERE payment_status <> 'unknown'")

    op.add_column(
        "categories",
        sa.Column("is_paid_experience", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.add_column("route_segments", sa.Column("price_amount", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "price_amount",
        "route_segments",
        "price_amount IS NULL OR price_amount >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("price_amount", "route_segments", type_="check")
    op.drop_column("route_segments", "price_amount")
    op.drop_column("categories", "is_paid_experience")
    op.drop_constraint("price_amounts", "places", type_="check")
    op.drop_constraint("price_source", "places", type_="check")
    op.drop_column("places", "price_hint")
    op.drop_column("places", "price_checked_at")
    op.drop_column("places", "price_source")

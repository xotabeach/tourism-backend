"""Give achievements an icon key and an explicit how-to-earn rule."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046_achievement_card"
down_revision: str | Sequence[str] | None = "0045_execution_awarded_points"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "achievements",
        sa.Column("how_to_earn", sa.String(length=240), nullable=False, server_default=""),
    )
    op.add_column(
        "achievements",
        sa.Column("icon_slug", sa.String(length=64), nullable=False, server_default=""),
    )
    # The seeded `description` is already the rule ("Пройти 48 км за неделю"),
    # so it is the honest starting value; editors can now write a separate
    # description without losing the rule text.
    op.execute("UPDATE achievements SET how_to_earn = description WHERE how_to_earn = ''")
    op.execute("UPDATE achievements SET icon_slug = slug WHERE icon_slug = ''")


def downgrade() -> None:
    op.drop_column("achievements", "icon_slug")
    op.drop_column("achievements", "how_to_earn")

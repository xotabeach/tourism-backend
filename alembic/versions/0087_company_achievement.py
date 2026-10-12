"""«Компания» is earned now: walk a route with a companion (BACKEND-61, 11b D16)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0087_company_achievement"
down_revision: str | Sequence[str] | None = "0086_companion_groups"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _text(value: str) -> None:
    op.execute(
        sa.text("UPDATE achievements SET how_to_earn = :value WHERE slug = 'group'").bindparams(
            value=value
        )
    )


def upgrade() -> None:
    _text("Пройти маршрут вместе с попутчиком")


def downgrade() -> None:
    _text("Совместные прохождения появятся позже")

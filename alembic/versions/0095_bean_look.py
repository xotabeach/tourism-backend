"""A companion character's build and clothes (BACKEND-71, FRONTEND-79).

``users.bean_kind`` is one of the four characters the designer drew;
``users.bean_outfit`` what it wears. Both None until the person picks:
the app then draws the build the account was given and no clothes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0095_bean_look"
down_revision: str | Sequence[str] | None = "0094_route_cost"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("bean_kind", sa.String(16), nullable=True))
    op.add_column("users", sa.Column("bean_outfit", sa.String(32), nullable=True))
    op.create_check_constraint(
        "bean_kind",
        "users",
        "bean_kind IS NULL OR bean_kind IN ('pin', 'tall', 'small', 'round')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_bean_kind", "users", type_="check")
    op.drop_column("users", "bean_outfit")
    op.drop_column("users", "bean_kind")

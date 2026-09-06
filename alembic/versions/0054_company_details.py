"""Company details singleton (mobile "О приложении" screen).

Replaces the static Dart `companyDetails` const with an admin-editable row,
seeded with the same defaults the Dart const shipped with (brand name and
working hours; the rest were already blank) so nothing regresses for a build
that hasn't picked up the mobile-side fetch yet.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0054_company_details"
down_revision: str | Sequence[str] | None = "0053_route_execution_pause"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "company_details"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("legal_name", sa.String(length=256), nullable=False),
        sa.Column("brand_name", sa.String(length=64), nullable=False),
        sa.Column("inn", sa.String(length=32), nullable=False),
        sa.Column("ogrn", sa.String(length=32), nullable=False),
        sa.Column("address", sa.String(length=256), nullable=False),
        sa.Column("email", sa.String(length=128), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("telegram", sa.String(length=64), nullable=False),
        sa.Column("working_hours", sa.String(length=128), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO company_details
                (id, legal_name, brand_name, inn, ogrn, address, email,
                 phone, telegram, working_hours, updated_at)
            VALUES
                (1, '', 'КрымТрип', '', '', '', '', '', '',
                 'Ежедневно с 10:00 до 20:00 (МСК)', now())
            """
        )
    )


def downgrade() -> None:
    op.drop_table(_TABLE)

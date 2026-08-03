"""Travel points are auto-awarded — not editable from admin forms."""

# revision identifiers, used by Alembic.
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_support_ticket_last_human"
down_revision: str | Sequence[str] | None = "0011_travel_points"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "support_tickets",
        sa.Column("last_human_author", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "support_tickets",
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_support_tickets_last_message_at",
        "support_tickets",
        ["last_message_at"],
    )
    op.create_index(
        "ix_support_tickets_last_human_author",
        "support_tickets",
        ["last_human_author"],
    )
    op.execute(
        sa.text(
            """
            UPDATE support_tickets AS t
            SET last_human_author = hum.author
            FROM (
              SELECT DISTINCT ON (ticket_id)
                ticket_id,
                author
              FROM support_messages
              WHERE author IN ('user', 'operator')
              ORDER BY ticket_id, created_at DESC
            ) AS hum
            WHERE t.id = hum.ticket_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE support_tickets AS t
            SET last_message_at = msg.created_at
            FROM (
              SELECT DISTINCT ON (ticket_id)
                ticket_id,
                created_at
              FROM support_messages
              ORDER BY ticket_id, created_at DESC
            ) AS msg
            WHERE t.id = msg.ticket_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE support_tickets
            SET last_message_at = updated_at
            WHERE last_message_at IS NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_support_tickets_last_human_author", table_name="support_tickets")
    op.drop_index("ix_support_tickets_last_message_at", table_name="support_tickets")
    op.drop_column("support_tickets", "last_message_at")
    op.drop_column("support_tickets", "last_human_author")

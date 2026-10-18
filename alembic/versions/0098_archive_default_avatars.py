"""Default avatar photos to the archive: the character's portrait instead (BACKEND-76).

Every account was given a random place photo as its avatar at sign-up.
The app now draws the person's companion character for anyone without a
picture of their own (FRONTEND-97), so those photos are archived, not
deleted: a downgrade brings them back where the person has not set an
avatar since.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0098_archive_default_avatars"
down_revision: str | Sequence[str] | None = "0097_not_reached_skip"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT = "Default profile avatar"


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE media_attachments
        SET status = 'archived', updated_at = now()
        WHERE entity_type = 'user' AND role = 'avatar' AND status = 'active'
          AND alt_text = '{_DEFAULT}'
        """
    )


def downgrade() -> None:
    # The newest default photo of each person back, unless they have an
    # avatar of their own by now (one active avatar per person).
    op.execute(
        f"""
        UPDATE media_attachments AS m
        SET status = 'active', updated_at = now()
        WHERE m.id IN (
            SELECT DISTINCT ON (d.entity_id) d.id
            FROM media_attachments AS d
            WHERE d.entity_type = 'user' AND d.role = 'avatar'
              AND d.status = 'archived' AND d.alt_text = '{_DEFAULT}'
              AND NOT EXISTS (
                SELECT 1 FROM media_attachments AS a
                WHERE a.entity_type = 'user' AND a.role = 'avatar'
                  AND a.status = 'active' AND a.entity_id = d.entity_id
              )
            ORDER BY d.entity_id, d.created_at DESC
        )
        """
    )

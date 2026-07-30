"""Canonical media_attachments table; migrate user/place media URLs."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_media_attachments"
down_revision: str | Sequence[str] | None = "0006_profile_support"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_attachments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("public_path", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("alt_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "entity_type IN ('user', 'place', 'route')",
            name="ck_media_attachments_entity_type",
        ),
        sa.CheckConstraint(
            "role IN ('avatar', 'cover', 'gallery')",
            name="ck_media_attachments_role",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_media_attachments_status",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_media_attachments_entity",
        "media_attachments",
        ["entity_type", "entity_id", "role"],
    )
    op.create_index("ix_media_attachments_status", "media_attachments", ["status"])
    op.create_index(
        "uq_media_attachments_one_avatar",
        "media_attachments",
        ["entity_type", "entity_id"],
        unique=True,
        postgresql_where=sa.text("role = 'avatar' AND status = 'active'"),
    )
    op.create_index(
        "uq_media_attachments_one_cover",
        "media_attachments",
        ["entity_type", "entity_id"],
        unique=True,
        postgresql_where=sa.text("role = 'cover' AND status = 'active'"),
    )

    # Migrate user avatar/cover URL columns into attachments.
    op.execute(
        """
        INSERT INTO media_attachments (
            id, entity_type, entity_id, role, storage_key, public_path,
            status, sort_order, created_at, updated_at
        )
        SELECT
            gen_random_uuid(),
            'user',
            id,
            'avatar',
            CASE
                WHEN avatar_url LIKE '/media/%' THEN substr(avatar_url, 8)
                ELSE avatar_url
            END,
            avatar_url,
            'active',
            0,
            COALESCE(created_at, now()),
            COALESCE(updated_at, now())
        FROM users
        WHERE avatar_url IS NOT NULL AND avatar_url <> ''
        """
    )
    op.execute(
        """
        INSERT INTO media_attachments (
            id, entity_type, entity_id, role, storage_key, public_path,
            status, sort_order, created_at, updated_at
        )
        SELECT
            gen_random_uuid(),
            'user',
            id,
            'cover',
            CASE
                WHEN cover_url LIKE '/media/%' THEN substr(cover_url, 8)
                ELSE cover_url
            END,
            cover_url,
            'active',
            0,
            COALESCE(created_at, now()),
            COALESCE(updated_at, now())
        FROM users
        WHERE cover_url IS NOT NULL AND cover_url <> ''
        """
    )
    op.drop_column("users", "avatar_url")
    op.drop_column("users", "cover_url")

    # Migrate place_images.source_url into attachments and link media_asset_id.
    op.execute(
        """
        WITH inserted AS (
            INSERT INTO media_attachments (
                id, entity_type, entity_id, role, storage_key, public_path,
                status, sort_order, alt_text, created_at, updated_at
            )
            SELECT
                gen_random_uuid(),
                'place',
                place_id,
                CASE WHEN is_cover THEN 'cover' ELSE 'gallery' END,
                CASE
                    WHEN source_url LIKE '/media/%' THEN substr(source_url, 8)
                    ELSE COALESCE(source_url, '')
                END,
                COALESCE(source_url, ''),
                CASE WHEN status = 'active' THEN 'active' ELSE 'archived' END,
                sort_order,
                alt_text,
                created_at,
                updated_at
            FROM place_images
            WHERE source_url IS NOT NULL AND source_url <> ''
            RETURNING id, entity_id, public_path, role
        )
        UPDATE place_images AS pi
        SET media_asset_id = inserted.id
        FROM inserted
        WHERE pi.place_id = inserted.entity_id
          AND pi.source_url = inserted.public_path
          AND (
            (pi.is_cover IS TRUE AND inserted.role = 'cover')
            OR (pi.is_cover IS FALSE AND inserted.role = 'gallery')
          )
        """
    )

    op.create_foreign_key(
        "fk_place_images_media_asset_id_media_attachments",
        "place_images",
        "media_attachments",
        ["media_asset_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_place_images_media_asset_id_media_attachments",
        "place_images",
        type_="foreignkey",
    )

    op.add_column("users", sa.Column("avatar_url", sa.String(length=512), nullable=True))
    op.add_column("users", sa.Column("cover_url", sa.String(length=512), nullable=True))
    op.execute(
        """
        UPDATE users AS u
        SET avatar_url = a.public_path
        FROM media_attachments AS a
        WHERE a.entity_type = 'user'
          AND a.entity_id = u.id
          AND a.role = 'avatar'
          AND a.status = 'active'
        """
    )
    op.execute(
        """
        UPDATE users AS u
        SET cover_url = a.public_path
        FROM media_attachments AS a
        WHERE a.entity_type = 'user'
          AND a.entity_id = u.id
          AND a.role = 'cover'
          AND a.status = 'active'
        """
    )

    op.drop_index("uq_media_attachments_one_cover", table_name="media_attachments")
    op.drop_index("uq_media_attachments_one_avatar", table_name="media_attachments")
    op.drop_index("ix_media_attachments_status", table_name="media_attachments")
    op.drop_index("ix_media_attachments_entity", table_name="media_attachments")
    op.drop_table("media_attachments")

"""Application helpers for media_attachments."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tourism_backend.api.errors import AppError
from tourism_backend.modules.media.infrastructure.models import (
    ENTITY_TYPES,
    ROLES,
    MediaAttachment,
)


def public_path_from_storage_key(storage_key: str) -> str:
    key = storage_key.lstrip("/")
    if key.startswith("media/"):
        key = key[len("media/") :]
    return f"/media/{key}"


def storage_key_from_public_path(public_path: str) -> str:
    if public_path.startswith("/media/"):
        return public_path[len("/media/") :]
    return public_path.lstrip("/")


async def resolve_urls(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_ids: list[UUID],
    role: str,
) -> dict[UUID, str]:
    if not entity_ids:
        return {}
    if entity_type not in ENTITY_TYPES or role not in ROLES:
        raise AppError(code="validation_error", message="Invalid media scope", status_code=400)
    stmt = select(MediaAttachment.entity_id, MediaAttachment.public_path).where(
        MediaAttachment.entity_type == entity_type,
        MediaAttachment.entity_id.in_(entity_ids),
        MediaAttachment.role == role,
        MediaAttachment.status == "active",
    )
    return {
        entity_id: public_path
        for entity_id, public_path in (await session.execute(stmt)).all()
        if public_path
    }


async def resolve_url(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: UUID,
    role: str,
) -> str | None:
    mapping = await resolve_urls(
        session,
        entity_type=entity_type,
        entity_ids=[entity_id],
        role=role,
    )
    return mapping.get(entity_id)


async def replace_attachment(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: UUID,
    role: str,
    storage_key: str,
    content_type: str | None = None,
    byte_size: int | None = None,
    width: int | None = None,
    height: int | None = None,
    checksum_sha256: str | None = None,
    uploaded_by_user_id: UUID | None = None,
    sort_order: int = 0,
    alt_text: str | None = None,
) -> MediaAttachment:
    if entity_type not in ENTITY_TYPES or role not in ROLES:
        raise AppError(code="validation_error", message="Invalid media scope", status_code=400)
    if role not in {"avatar", "cover"}:
        raise AppError(
            code="validation_error",
            message="replace_attachment supports avatar/cover only",
            status_code=400,
        )

    now = datetime.now(UTC)
    await session.execute(
        update(MediaAttachment)
        .where(
            MediaAttachment.entity_type == entity_type,
            MediaAttachment.entity_id == entity_id,
            MediaAttachment.role == role,
            MediaAttachment.status == "active",
        )
        .values(status="archived", updated_at=now)
    )

    attachment = MediaAttachment(
        id=uuid4(),
        entity_type=entity_type,
        entity_id=entity_id,
        role=role,
        storage_key=storage_key_from_public_path(storage_key)
        if storage_key.startswith("/media/")
        else storage_key.lstrip("/"),
        public_path=public_path_from_storage_key(storage_key),
        content_type=content_type,
        byte_size=byte_size,
        width=width,
        height=height,
        checksum_sha256=checksum_sha256,
        status="active",
        uploaded_by_user_id=uploaded_by_user_id,
        sort_order=sort_order,
        alt_text=alt_text,
        created_at=now,
        updated_at=now,
    )
    session.add(attachment)
    await session.flush()
    return attachment


def upsert_place_file_attachment(
    session: Session,
    *,
    place_id: UUID,
    role: str,
    public_path: str,
    sort_order: int = 0,
    alt_text: str | None = None,
    status: str = "active",
    content_type: str | None = None,
    byte_size: int | None = None,
    width: int | None = None,
    height: int | None = None,
    checksum_sha256: str | None = None,
) -> MediaAttachment:
    """Sync helper for seed / import scripts."""
    if role not in ROLES:
        raise ValueError(f"invalid role: {role}")
    now = datetime.now(UTC)
    existing = session.scalar(
        select(MediaAttachment).where(
            MediaAttachment.entity_type == "place",
            MediaAttachment.entity_id == place_id,
            MediaAttachment.public_path == public_path,
            MediaAttachment.role == role,
        )
    )
    if existing is None and role == "cover":
        # Prefer matching active cover for this place when path changed.
        existing = session.scalar(
            select(MediaAttachment).where(
                MediaAttachment.entity_type == "place",
                MediaAttachment.entity_id == place_id,
                MediaAttachment.role == "cover",
                MediaAttachment.status == "active",
            )
        )

    if role == "cover" and status == "active":
        archive_stmt = select(MediaAttachment).where(
            MediaAttachment.entity_type == "place",
            MediaAttachment.entity_id == place_id,
            MediaAttachment.role == "cover",
            MediaAttachment.status == "active",
        )
        if existing is not None:
            archive_stmt = archive_stmt.where(MediaAttachment.id != existing.id)
        for row in session.scalars(archive_stmt).all():
            row.status = "archived"
            row.updated_at = now

    if existing is None:
        existing = MediaAttachment(
            id=uuid4(),
            entity_type="place",
            entity_id=place_id,
            role=role,
            storage_key=storage_key_from_public_path(public_path),
            public_path=public_path,
            created_at=now,
            updated_at=now,
        )
        session.add(existing)

    existing.storage_key = storage_key_from_public_path(public_path)
    existing.public_path = public_path
    existing.role = role
    existing.status = status
    existing.sort_order = sort_order
    existing.alt_text = alt_text
    if content_type is not None:
        existing.content_type = content_type
    if byte_size is not None:
        existing.byte_size = byte_size
    if width is not None:
        existing.width = width
    if height is not None:
        existing.height = height
    if checksum_sha256 is not None:
        existing.checksum_sha256 = checksum_sha256
    existing.updated_at = now
    session.flush()
    return existing


class ReusableCover:
    def __init__(self, storage_key: str, public_path: str, content_type: str | None) -> None:
        self.storage_key = storage_key
        self.public_path = public_path
        self.content_type = content_type


async def list_reusable_covers(session: AsyncSession, *, limit: int = 40) -> list[ReusableCover]:
    """Active route/place covers that can be reused as default profile media."""
    rows = (
        await session.execute(
            select(
                MediaAttachment.storage_key,
                MediaAttachment.public_path,
                MediaAttachment.content_type,
            )
            .where(
                MediaAttachment.status == "active",
                MediaAttachment.role == "cover",
                MediaAttachment.entity_type.in_(("route", "place")),
            )
            .limit(limit)
        )
    ).all()
    return [
        ReusableCover(storage_key=key, public_path=path, content_type=content_type)
        for key, path, content_type in rows
        if key and path
    ]

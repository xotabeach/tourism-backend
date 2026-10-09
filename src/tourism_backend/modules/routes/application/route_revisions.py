"""Versions of a published route (spec 15, D5, D6, D17).

A published route stays in the catalogue while its author edits it. The edit
is a route row of its own (``revision_of_route_id``): the editor, the days,
the media and the moderation queue all work on it as on any draft. Once a
moderator approves it, its content replaces the published route's, which
keeps its id, and with it its reviews, rating, favourites and runs.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.routes.application.structure import refresh_route_structure
from tourism_backend.modules.routes.infrastructure.models import (
    Route,
    RouteDay,
    RouteSegment,
    RouteStop,
)

RevisionStatus = Literal["draft", "pending_review", "rejected"]

#: What a route is, not what it says: never copied between a route and its
#: edit. Every other column is content and travels with the version, so a
#: content column added later is versioned without touching this module.
_NOT_CONTENT = frozenset(
    {
        "id",
        "slug",
        "owner_user_id",
        "source",
        "visibility",
        "lifecycle_status",
        "publication_status",
        "client_draft_id",
        "rejection_reason",
        "moderator_note",
        "popularity",
        "popularity_people",
        "is_popular",
        "popularity_updated_at",
        "revision_of_route_id",
        "content_updated_at",
        "created_at",
        "updated_at",
    }
)


async def _copy_content(session: AsyncSession, source: Route, target: Route) -> None:
    for column in Route.__table__.columns:
        if column.name not in _NOT_CONTENT and column.name != "geometry":
            setattr(target, column.name, getattr(source, column.name))
    await session.flush()
    # The line is copied inside the database: reading it into Python and
    # writing it back would need a geometry library for nothing.
    await session.execute(
        update(Route)
        .where(Route.id == target.id)
        .values(geometry=select(Route.geometry).where(Route.id == source.id).scalar_subquery())
    )
    await session.refresh(target, ["geometry"])


async def revision_of(session: AsyncSession, route_id: UUID) -> Route | None:
    """The edit waiting beside a published route, if there is one."""
    revision: Route | None = await session.scalar(
        select(Route).where(
            Route.revision_of_route_id == route_id,
            Route.publication_status != "deleted",
        )
    )
    return revision


async def revisions_of(session: AsyncSession, route_ids: Sequence[UUID]) -> dict[UUID, Route]:
    if not route_ids:
        return {}
    rows = await session.scalars(
        select(Route).where(
            Route.revision_of_route_id.in_(route_ids),
            Route.publication_status != "deleted",
        )
    )
    return {row.revision_of_route_id: row for row in rows if row.revision_of_route_id}


def _active_media(route_id: UUID):  # type: ignore[no-untyped-def]
    return (
        select(MediaAttachment)
        .where(
            MediaAttachment.entity_type == "route",
            MediaAttachment.entity_id == route_id,
            MediaAttachment.status == "active",
        )
        .order_by(MediaAttachment.sort_order, MediaAttachment.created_at)
    )


async def ensure_revision(session: AsyncSession, route: Route, *, now: datetime) -> Route:
    """The edit of ``route``, started as its exact copy when there is none."""
    existing = await revision_of(session, route.id)
    if existing is not None:
        return existing
    revision_id = uuid4()
    revision = Route(
        id=revision_id,
        slug=f"revision-{revision_id.hex}",
        owner_user_id=route.owner_user_id,
        source=route.source,
        visibility="private",
        lifecycle_status="draft",
        publication_status="draft",
        revision_of_route_id=route.id,
        created_at=now,
        updated_at=now,
    )
    session.add(revision)
    await _copy_content(session, route, revision)
    stops = await session.scalars(
        select(RouteStop).where(RouteStop.route_id == route.id).order_by(RouteStop.position)
    )
    for stop in stops:
        session.add(
            RouteStop(
                id=uuid4(),
                route_id=revision.id,
                place_id=stop.place_id,
                place_entrance_id=stop.place_entrance_id,
                position=stop.position,
                visit_duration_minutes=stop.visit_duration_minutes,
                note=stop.note,
                is_optional=stop.is_optional,
                time_of_day=stop.time_of_day,
                created_at=now,
                updated_at=now,
            )
        )
    for item in await session.scalars(_active_media(route.id)):
        session.add(
            MediaAttachment(
                id=uuid4(),
                entity_type="route",
                entity_id=revision.id,
                role=item.role,
                storage_key=item.storage_key,
                public_path=item.public_path,
                content_type=item.content_type,
                byte_size=item.byte_size,
                width=item.width,
                height=item.height,
                checksum_sha256=item.checksum_sha256,
                status="active",
                uploaded_by_user_id=item.uploaded_by_user_id,
                sort_order=item.sort_order,
                alt_text=item.alt_text,
                copied_from_id=item.id,
                created_at=now,
                updated_at=now,
            )
        )
    await session.flush()
    # Days and segments are derived from the stops; day boundaries are kept
    # by place, so they come out the same as on the published route.
    await refresh_route_structure(session, revision)
    return revision


async def apply_revision(session: AsyncSession, revision: Route, *, now: datetime) -> Route:
    """Replace the route's content with its edit; the edit row is retired.

    The caller decides what the route's status is afterwards: an approval
    leaves it published, a withdrawal has already made it a draft.
    """
    target = await session.get(Route, revision.revision_of_route_id)
    if target is None:  # the foreign key cascades, so this cannot happen
        raise LookupError("revision without its route")
    await _copy_content(session, revision, target)
    # The route's own stops go first: ``(route_id, position)`` is unique, and
    # days and segments point at stop rows.
    for model in (RouteSegment, RouteDay, RouteStop):
        await session.execute(delete(model).where(model.route_id == target.id))
    await session.flush()
    for model in (RouteStop, RouteDay, RouteSegment):
        await session.execute(
            update(model).where(model.route_id == revision.id).values(route_id=target.id)
        )
    # Media: demote the old cover before the new one arrives, there is one
    # active cover per route.
    await session.execute(
        update(MediaAttachment)
        .where(
            MediaAttachment.entity_type == "route",
            MediaAttachment.entity_id == target.id,
            MediaAttachment.status == "active",
        )
        .values(status="archived", updated_at=now)
    )
    await session.flush()
    await session.execute(
        update(MediaAttachment)
        .where(
            MediaAttachment.entity_type == "route",
            MediaAttachment.entity_id == revision.id,
            MediaAttachment.status == "active",
        )
        .values(entity_id=target.id, updated_at=now)
    )
    target.content_updated_at = now
    target.updated_at = now
    revision.publication_status = "deleted"
    revision.lifecycle_status = "archived"
    revision.updated_at = now
    await session.flush()
    return target


async def retire_revision(session: AsyncSession, revision: Route, *, now: datetime) -> None:
    """Throw the edit away; the published route is untouched."""
    revision.publication_status = "deleted"
    revision.lifecycle_status = "archived"
    revision.updated_at = now
    await session.execute(
        update(MediaAttachment)
        .where(
            MediaAttachment.entity_type == "route",
            MediaAttachment.entity_id == revision.id,
            MediaAttachment.status == "active",
        )
        .values(status="archived", updated_at=now)
    )
    await session.flush()


@dataclass(frozen=True)
class VersionFacts:
    """What a moderator compares between two versions of a route."""

    name: str
    description: str
    stops: tuple[str, ...]
    media: tuple[str, ...]
    transport_mode: str | None = None
    difficulty_level: int | None = None
    days: int = 1


@dataclass(frozen=True)
class VersionDiff:
    name: tuple[str, str] | None = None
    description: tuple[str, str] | None = None
    stops_added: tuple[str, ...] = ()
    stops_removed: tuple[str, ...] = ()
    stops_reordered: bool = False
    stops_before: tuple[str, ...] = ()
    stops_after: tuple[str, ...] = ()
    media_added: int = 0
    media_removed: int = 0
    media_reordered: bool = False
    other: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return self == VersionDiff(stops_before=self.stops_before, stops_after=self.stops_after)

    @property
    def stops_changed_share(self) -> int:
        """Percent of the published stops that are gone: a hint for
        «опубликуйте как новый маршрут»."""
        if not self.stops_before:
            return 0
        return round(100 * len(self.stops_removed) / len(self.stops_before))


def _show(value: object) -> str:
    return "нет" if value is None else str(value)


def diff_versions(before: VersionFacts, after: VersionFacts) -> VersionDiff:
    """Pure: what changed from the published version to the edit."""
    kept_before = [name for name in before.stops if name in after.stops]
    kept_after = [name for name in after.stops if name in before.stops]
    media_kept_before = [key for key in before.media if key in after.media]
    media_kept_after = [key for key in after.media if key in before.media]
    other = [
        (label, _show(old), _show(new))
        for label, old, new in (
            ("Способ передвижения", before.transport_mode, after.transport_mode),
            ("Сложность", before.difficulty_level, after.difficulty_level),
            ("Дней", before.days, after.days),
        )
        if old != new
    ]
    return VersionDiff(
        name=(before.name, after.name) if before.name != after.name else None,
        description=(
            (before.description, after.description)
            if before.description != after.description
            else None
        ),
        stops_added=tuple(name for name in after.stops if name not in before.stops),
        stops_removed=tuple(name for name in before.stops if name not in after.stops),
        stops_reordered=kept_before != kept_after,
        stops_before=before.stops,
        stops_after=after.stops,
        media_added=sum(1 for key in after.media if key not in before.media),
        media_removed=sum(1 for key in before.media if key not in after.media),
        media_reordered=media_kept_before != media_kept_after,
        other=tuple(other),
    )


async def version_facts(session: AsyncSession, route: Route) -> VersionFacts:
    stops = (
        await session.execute(
            select(Place.name)
            .join(RouteStop, RouteStop.place_id == Place.id)
            .where(RouteStop.route_id == route.id)
            .order_by(RouteStop.position)
        )
    ).scalars()
    media = [item.storage_key for item in await session.scalars(_active_media(route.id))]
    days = len(
        (await session.scalars(select(RouteDay.id).where(RouteDay.route_id == route.id))).all()
    )
    return VersionFacts(
        name=route.name,
        description=route.description or "",
        stops=tuple(stops),
        media=tuple(media),
        transport_mode=route.transport_mode,
        difficulty_level=route.difficulty_level,
        days=max(days, 1),
    )


async def revision_diff(session: AsyncSession, revision: Route) -> VersionDiff | None:
    target = await session.get(Route, revision.revision_of_route_id)
    if target is None:
        return None
    return diff_versions(
        await version_facts(session, target), await version_facts(session, revision)
    )

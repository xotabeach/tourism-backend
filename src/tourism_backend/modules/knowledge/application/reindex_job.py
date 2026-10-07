"""Nightly reconciliation of the knowledge index (spec 18, D13).

Once a night, when almost nobody is in the app, the index is brought in line
with what is published: new and changed texts are chunked and embedded, and
chunks of places and routes that are no longer shown are dropped. It runs
inside the API process on purpose: the embedding model is already loaded
there and answers queries every day, while a second process with its own
copy of the model starved the API of CPU and then hung (2026-10-08).

Only what changed is embedded, one chunk at a time with a pause, so a
normal night costs a few seconds of one core. The first run after a model
switch re-embeds everything the same slow way.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, literal_column, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tourism_backend.config import Settings
from tourism_backend.modules.geography.infrastructure.models import Locality
from tourism_backend.modules.knowledge.application.chunker import (
    ChunkCandidate,
    chunk_place_markdown,
    chunk_route_markdown,
    content_hash,
)
from tourism_backend.modules.knowledge.application.documents import (
    has_real_description,
    route_description,
    route_facts,
)
from tourism_backend.modules.knowledge.application.embedder import (
    EmbeddingProvider,
    build_embedder,
)
from tourism_backend.modules.knowledge.infrastructure.models import KnowledgeChunk
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.routes.infrastructure.models import Route, RouteDay, RouteStop

_logger = logging.getLogger("tourism_backend.knowledge_reindex")

# One process at a time reconciles (several uvicorn workers may run this).
_LOCK_KEY = 180_018
_SOURCE = "internal"
_ZONE = ZoneInfo("Europe/Moscow")
_COMMIT_EVERY = 20


@dataclass(frozen=True, slots=True)
class IndexedChunk:
    """A chunk as the index holds it now."""

    id: UUID
    content_hash: str
    embedding_model: str | None
    embedded: bool


@dataclass(frozen=True, slots=True)
class ReindexPlan:
    insert: list[ChunkCandidate]
    update: list[tuple[UUID, ChunkCandidate]]
    embed: list[tuple[UUID, ChunkCandidate]]
    stale: list[UUID]


def plan_reindex(
    existing: dict[tuple[str, int], IndexedChunk],
    candidates: list[ChunkCandidate],
    *,
    model: str,
) -> ReindexPlan:
    """What to write so the index matches the candidates.

    A chunk is embedded again when its text changed, when it has no vector,
    or when its vector came from another model: such a vector is never
    compared with a query, so the chunk would be invisible to the search.
    Inserted chunks are embedded too; the caller knows their ids.
    """
    insert: list[ChunkCandidate] = []
    update: list[tuple[UUID, ChunkCandidate]] = []
    embed: list[tuple[UUID, ChunkCandidate]] = []
    wanted: set[tuple[str, int]] = set()
    for candidate in candidates:
        key = (candidate.doc_id, candidate.chunk_seq)
        wanted.add(key)
        current = existing.get(key)
        if current is None:
            insert.append(candidate)
        elif current.content_hash != content_hash(candidate.body):
            update.append((current.id, candidate))
        elif not current.embedded or current.embedding_model != model:
            embed.append((current.id, candidate))
    stale = [chunk.id for key, chunk in existing.items() if key not in wanted]
    return ReindexPlan(insert=insert, update=update, embed=embed, stale=stale)


def seconds_until(hour: int, minute: int, *, now: datetime) -> float:
    """Seconds from ``now`` to the next HH:MM in Moscow time."""
    local = now.astimezone(_ZONE)
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return (target - local).total_seconds()


def _attrs(candidate: ChunkCandidate, *, route_id: str | None) -> dict[str, object]:
    return {
        "doc_id": candidate.doc_id,
        "chunk_seq": candidate.chunk_seq,
        "source": candidate.source,
        "license": candidate.license_note,
        "place_id": UUID(candidate.place_id) if candidate.place_id else None,
        "title": candidate.title,
        "region": candidate.region,
        "locality": candidate.locality,
        "lang": "ru",
        "content_type": candidate.content_type,
        "body": candidate.body,
        "content_hash": content_hash(candidate.body),
        "parsed_at": datetime.now(UTC),
        "ttl_days": 365,
        "payload": (
            {"source": candidate.source, "route_id": route_id}
            if route_id
            else {"source": candidate.source, "place_id": candidate.place_id}
        ),
    }


async def _candidates(session: AsyncSession) -> list[ChunkCandidate]:
    out: list[ChunkCandidate] = []
    places = await session.execute(
        select(Place, Locality.name)
        .outerjoin(Locality, Locality.id == Place.locality_id)
        .where(Place.publication_status == "published", Place.merged_into_place_id.is_(None))
        .order_by(Place.name)
    )
    for place, locality in places.all():
        out += chunk_place_markdown(
            place_id=str(place.id),
            name=place.name,
            short_description=place.short_description,
            description=place.description if has_real_description(place) else None,
            locality=locality,
            source=_SOURCE,
        )
    routes = (
        await session.scalars(
            select(Route)
            .where(
                Route.publication_status == "published",
                Route.visibility == "public",
                Route.lifecycle_status == "active",
            )
            .order_by(Route.name)
        )
    ).all()
    for route in routes:
        stops = (
            await session.execute(
                select(RouteStop.id, RouteStop.position, Place.name, Locality.name)
                .join(Place, Place.id == RouteStop.place_id)
                .outerjoin(Locality, Locality.id == Place.locality_id)
                .where(RouteStop.route_id == route.id)
                .order_by(RouteStop.position)
            )
        ).all()
        days = (
            await session.scalars(
                select(RouteDay).where(RouteDay.route_id == route.id).order_by(RouteDay.day_index)
            )
        ).all()
        position = {stop[0]: stop[1] for stop in stops}
        facts = route_facts(
            route,
            stops=[(stop[1], stop[2]) for stop in stops],
            days=[
                (
                    day.day_index,
                    position.get(day.first_stop_id, 0),
                    position.get(day.last_stop_id, 0),
                    day.overnight_note,
                )
                for day in days
            ],
        )
        out += chunk_route_markdown(
            route_id=str(route.id),
            name=route.name,
            short_description=route.short_description,
            description=route_description(route.description, facts),
            locality=next((stop[3] for stop in stops if stop[3]), None),
            source=_SOURCE,
        )
    return out


async def _write_embedding(
    session: AsyncSession, chunk_id: UUID, vector: list[float], model: str
) -> None:
    vec = "[" + ",".join(f"{v:.5f}" for v in vector) + "]"
    await session.execute(
        text(
            "UPDATE knowledge_chunks SET embedding = CAST(:vec AS vector), "
            "embedding_model = :model WHERE id = :id"
        ),
        {"vec": vec, "model": model, "id": str(chunk_id)},
    )


async def reconcile_once(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: EmbeddingProvider,
    *,
    pause_seconds: float,
) -> dict[str, int] | None:
    """Bring the index in line with what is published; None when another
    process holds the lock."""
    model = embedder.model_id
    async with session_factory() as session:
        got_lock = await session.scalar(select(func.pg_try_advisory_lock(_LOCK_KEY)))
        if not got_lock:
            return None
        try:
            candidates = await _candidates(session)
            rows = await session.execute(
                select(
                    KnowledgeChunk.id,
                    KnowledgeChunk.doc_id,
                    KnowledgeChunk.chunk_seq,
                    KnowledgeChunk.content_hash,
                    # The vector columns live outside the ORM model
                    # (pgvector, migration 0032).
                    literal_column("embedding_model"),
                    literal_column("embedding IS NOT NULL"),
                ).where(
                    KnowledgeChunk.source == _SOURCE,
                    func.split_part(KnowledgeChunk.doc_id, ":", 1).in_(("place", "route")),
                )
            )
            existing = {
                (doc_id, seq): IndexedChunk(chunk_id, digest, chunk_model, bool(embedded))
                for chunk_id, doc_id, seq, digest, chunk_model, embedded in rows.all()
            }
            plan = plan_reindex(existing, candidates, model=model)

            to_embed = list(plan.embed)
            for candidate in plan.insert:
                chunk_id = uuid4()
                session.add(
                    KnowledgeChunk(id=chunk_id, **_attrs(candidate, route_id=_route_id(candidate)))
                )
                to_embed.append((chunk_id, candidate))
            for chunk_id, candidate in plan.update:
                await session.execute(
                    KnowledgeChunk.__table__.update()
                    .where(KnowledgeChunk.id == chunk_id)
                    .values(
                        **_attrs(candidate, route_id=_route_id(candidate)),
                        updated_at=datetime.now(UTC),
                    )
                )
                to_embed.append((chunk_id, candidate))
            if plan.stale:
                await session.execute(
                    KnowledgeChunk.__table__.delete().where(KnowledgeChunk.id.in_(plan.stale))
                )
            await session.commit()

            embedded = failed = 0
            for number, (chunk_id, candidate) in enumerate(to_embed, start=1):
                try:
                    vector = await embedder.embed(f"{candidate.title} {candidate.body}")
                    await _write_embedding(session, chunk_id, vector, model)
                    embedded += 1
                except Exception:  # noqa: BLE001 — one bad chunk must not stop the rest
                    await session.rollback()
                    failed += 1
                    _logger.exception("knowledge_embed_failed", extra={"chunk_id": str(chunk_id)})
                if number % _COMMIT_EVERY == 0:
                    await session.commit()
                # Leave the CPU to the requests of whoever is awake.
                await asyncio.sleep(pause_seconds)
            await session.commit()
            return {
                "scanned": len(candidates),
                "inserted": len(plan.insert),
                "updated": len(plan.update),
                "removed": len(plan.stale),
                "embedded": embedded,
                "embed_failed": failed,
            }
        finally:
            # A failed statement leaves the transaction aborted; the lock is
            # released in a fresh one either way.
            await session.rollback()
            await session.execute(select(func.pg_advisory_unlock(_LOCK_KEY)))
            await session.commit()


def _route_id(candidate: ChunkCandidate) -> str | None:
    kind, _, key = candidate.doc_id.partition(":")
    return key if kind == "route" else None


async def poll_knowledge_reindex(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    if not settings.rag_enabled or not settings.rag_reindex_enabled:
        return
    embedder = build_embedder(settings)
    while True:
        await asyncio.sleep(
            seconds_until(
                settings.rag_reindex_hour, settings.rag_reindex_minute, now=datetime.now(UTC)
            )
        )
        try:
            result = await reconcile_once(
                session_factory, embedder, pause_seconds=settings.rag_reindex_pause_seconds
            )
            if result is not None:
                _logger.info("knowledge_reindexed", extra=result)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a failed night must not stop the next one
            _logger.exception("knowledge_reindex_failed")
        # Past the minute it started in, so the same night is not run twice.
        await asyncio.sleep(90)


async def stop_knowledge_reindex(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

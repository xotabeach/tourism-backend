#!/usr/bin/env python3
"""Index narrative content (places/routes) into knowledge_chunks for RAG.

Dry-run by default: prints counts that would be inserted/updated, then a
summary. With --apply, chunks are upserted by (doc_id, chunk_seq).

With --embed, also writes pgvector embeddings via whatever embedder
RAG_EMBEDDING_MODEL selects (build_embedder — hash-v1 bootstrap embedder by
default, or an LM Studio model when configured; same vectors the retriever
uses).

With --reembed-all, re-embeds every existing knowledge_chunks row with the
currently configured embedder instead of touching chunk content — the move
after switching RAG_EMBEDDING_MODEL from hash-v1 to a real model.

Examples:
  uv run python scripts/ingest_knowledge.py --limit 300
  uv run python scripts/ingest_knowledge.py --apply --embed
  uv run python scripts/ingest_knowledge.py --apply --limit 1000 --source internal
  uv run python scripts/ingest_knowledge.py --apply --reembed-all
  uv run python scripts/ingest_knowledge.py --apply --embed --prune --limit 5000
  nice -n 19 python scripts/ingest_knowledge.py --apply --embed --prune --limit 5000 --threads 1
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from tourism_backend.config import get_settings
from tourism_backend.modules.geography.infrastructure import models as _geo
from tourism_backend.modules.knowledge.application.chunker import (
    chunk_place_markdown,
    chunk_route_markdown,
    content_hash,
)
from tourism_backend.modules.knowledge.application.embedder import (
    EmbeddingProvider,
    SentenceTransformerEmbeddingProvider,
    build_embedder,
)
from tourism_backend.modules.knowledge.infrastructure.models import KnowledgeChunk
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.routes.infrastructure.models import Route

#: Cap concurrent embedding requests against a remote embedding server (a
#: home-lab LM Studio has one GPU). The in-process model gets one at a time.
_EMBED_CONCURRENCY = 4

_ = _geo


_BOILERPLATE_PROMPT_VERSIONS = frozenset({"heuristic-v1"})
# The literal marker `content_enrichment.py` writes into `description` when
# no source text existed. Checked directly rather than trusted to imply
# `prompt_version` == "heuristic-v1": three seed places (Ливадийский дворец,
# Херсонес Таврический, Долина привидений) carry this exact text in
# `description` with `content_enrichment_status = "missing"` and no
# `prompt_version` at all — their status was reset at some point without
# clearing the stale text it had written. Metadata drifted; the string in
# the column that actually gets indexed did not.
_BOILERPLATE_MARKER = "Описание сгенерировано автоматически как черновик"


def _has_real_description(place: Place) -> bool:
    """True unless `description` is the templated placeholder.

    `content_enrichment.prompt_version` distinguishes the two heuristics in
    `content_enrichment.py`: "heuristic-wikipedia-v1" wraps a real extract,
    "heuristic-v1" is the "Описание сгенерировано автоматически..." template
    used when no source text existed. 82.6% of `generated_draft` places are
    the template (measured 2026-09-03) — indexing it teaches the retriever
    nothing about the place and puts "требует редакционной проверки" one
    retrieval away from a user's screen.
    """
    description = place.description or ""
    if _BOILERPLATE_MARKER in description:
        return False
    enrichment = place.content_enrichment
    if not isinstance(enrichment, dict):
        return True
    return enrichment.get("prompt_version") not in _BOILERPLATE_PROMPT_VERSIONS


def _iter_places(session: Session, *, limit: int) -> list[tuple[Place, str | None]]:
    return [
        (place, _locality_name(session, place.locality_id))
        for place in session.scalars(
            select(Place)
            .where(Place.publication_status == "published")
            .order_by(Place.name)
            .limit(limit)
        )
    ]


def _locality_name(session: Session, locality_id: UUID | None) -> str | None:
    from tourism_backend.modules.geography.infrastructure.models import Locality

    if locality_id is None:
        return None
    row = session.get(Locality, locality_id)
    return row.name if row is not None else None


def _iter_routes(session: Session, *, limit: int) -> list[tuple[Route, str | None]]:
    routes = session.scalars(
        select(Route)
        .where(
            Route.publication_status == "published",
            Route.visibility == "public",
            # An archived route is out of the catalog; the agent must not
            # offer it either.
            Route.lifecycle_status == "active",
        )
        .order_by(Route.name)
        .limit(limit)
    ).all()
    out: list[tuple[Route, str | None]] = []
    for route in routes:
        out.append((route, _route_locality(session, route.id)))
    return out


def _route_locality(session: Session, route_id: UUID) -> str | None:
    from tourism_backend.modules.geography.infrastructure.models import Locality
    from tourism_backend.modules.places.infrastructure.models import Place
    from tourism_backend.modules.routes.infrastructure.models import RouteStop

    row = session.execute(
        select(Locality.name)
        .select_from(RouteStop)
        .join(Place, Place.id == RouteStop.place_id)
        .join(Locality, Locality.id == Place.locality_id)
        .where(RouteStop.route_id == route_id)
        .order_by(RouteStop.position)
        .limit(1)
    ).first()
    return row[0] if row is not None else None


_LEVELS = {1: "лёгкий", 2: "несложный", 3: "средней сложности", 4: "трудный", 5: "очень трудный"}


def _route_facts(session: Session, route: Route) -> str:
    """The route's stops and days from the database, as `route_facts` text."""
    from tourism_backend.modules.places.infrastructure.models import Place
    from tourism_backend.modules.routes.infrastructure.models import RouteDay, RouteStop

    stops = session.execute(
        select(RouteStop.id, RouteStop.position, Place.name)
        .join(Place, Place.id == RouteStop.place_id)
        .where(RouteStop.route_id == route.id)
        .order_by(RouteStop.position)
    ).all()
    days = session.scalars(
        select(RouteDay).where(RouteDay.route_id == route.id).order_by(RouteDay.day_index)
    ).all()
    position = {stop.id: stop.position for stop in stops}
    return route_facts(
        route,
        stops=[(stop.position, stop.name) for stop in stops],
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


def route_facts(
    route: Any,
    *,
    stops: list[tuple[int, str]],
    days: list[tuple[int, int, int, str | None]],
) -> str:
    """What the agent matches a request against besides the prose: how the
    route is travelled, how long and hard it is, its tags and its stops by
    day. Numbers come from the stored calculation, never from the text.

    ``stops`` are (position, name); ``days`` are (index, first position,
    last position, overnight note)."""
    lines = ["## Параметры"]
    lines.append("Способ: " + ("на машине" if route.base_mode != "walk" else "пешком"))
    if len(days) > 1:
        lines.append(f"Дней: {len(days)} (многодневный маршрут)")
    elif route.estimated_duration_minutes:
        hours = route.estimated_duration_minutes / 60
        lines.append("Длительность: " + ("полдня" if hours <= 4.5 else "один день"))
    if route.distance_meters:
        lines.append(f"Длина пути: {route.distance_meters / 1000:.1f} км")
    if route.difficulty_level:
        lines.append(
            f"Сложность: {route.difficulty_level} из 5, {_LEVELS.get(route.difficulty_level, '')}"
        )
    filters = (route.accessibility or {}).get("filters") or []
    tags = [str(tag) for tag in filters if isinstance(tag, str)]
    if route.is_seaside and "Море" not in tags:
        tags.append("Море")
    if tags:
        lines.append("Метки: " + ", ".join(tags))
    if route.suitable_for_children:
        lines.append("Подходит для поездки с детьми")
    if route.source == "editorial":
        lines.append("Автор: редакция КРЫМТРИП")
    lines.append("\n## Точки маршрута")
    if len(days) > 1:
        for index, first, last, night in days:
            names = [name for position, name in stops if first <= position <= last]
            line = f"День {index}: " + ", ".join(names)
            if night:
                line += f". {night}"
            lines.append(line)
    else:
        lines.append(", ".join(name for _position, name in stops))
    return "\n".join(lines)


def _prune(session: Session, *, keep: set[tuple[str, int]], source: str, dry_run: bool) -> int:
    """Drop what is no longer indexed: every chunk of an unpublished or merged
    place and of an archived or deleted route, and a leftover chunk of a
    document that now splits into fewer parts. Returns the number of chunks."""
    stale = [
        chunk_id
        for chunk_id, doc_id, seq in session.execute(
            select(KnowledgeChunk.id, KnowledgeChunk.doc_id, KnowledgeChunk.chunk_seq).where(
                KnowledgeChunk.source == source
            )
        )
        if doc_id.split(":", 1)[0] in {"place", "route"} and (doc_id, seq) not in keep
    ]
    if stale and not dry_run:
        session.execute(KnowledgeChunk.__table__.delete().where(KnowledgeChunk.id.in_(stale)))
    return len(stale)


def _upsert_chunk(
    session: Session,
    *,
    attrs: dict[str, object],
    dry_run: bool,
) -> tuple[str, UUID | None]:
    key = {"doc_id": attrs["doc_id"], "chunk_seq": attrs["chunk_seq"]}
    existing = session.scalar(
        select(KnowledgeChunk).where(
            KnowledgeChunk.doc_id == key["doc_id"],
            KnowledgeChunk.chunk_seq == key["chunk_seq"],
        )
    )
    if existing is not None:
        if existing.content_hash != attrs["content_hash"]:
            if not dry_run:
                for field, value in attrs.items():
                    setattr(existing, field, value)
                existing.updated_at = datetime.now(UTC)
            return "updated", existing.id
        return "unchanged", existing.id
    if not dry_run:
        chunk_id = uuid4()
        session.add(KnowledgeChunk(id=chunk_id, **attrs))
        return "inserted", chunk_id
    return "inserted", None


def _write_embedding(
    session: Session,
    *,
    chunk_id: UUID,
    vector: list[float],
    model: str,
) -> None:
    vec = "[" + ",".join(f"{v:.5f}" for v in vector) + "]"
    session.execute(
        text(
            "UPDATE knowledge_chunks SET embedding = CAST(:vec AS vector), "
            "embedding_model = :model WHERE id = :id"
        ),
        {"vec": vec, "model": model, "id": str(chunk_id)},
    )


async def _embed_batch(
    embedder: EmbeddingProvider,
    pending: list[tuple[UUID, str, str]],
    concurrency: int = _EMBED_CONCURRENCY,
) -> list[tuple[UUID, list[float]]]:
    """Embed (chunk_id, title, body) triples concurrently, capped.

    A chunk whose embed call fails (network hiccup, provider down) is
    skipped rather than aborting the whole batch — it keeps whatever
    embedding it had before (or none), and a later run picks it up again.
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def _one(chunk_id: UUID, title: str, body: str) -> tuple[UUID, list[float]] | None:
        async with semaphore:
            try:
                vector = await embedder.embed(f"{title} {body}")
            except Exception as exc:  # noqa: BLE001 — one bad chunk must not sink the batch
                print(f"  ! embed failed for {chunk_id}: {exc}")
                return None
            return chunk_id, vector

    results = await asyncio.gather(*(_one(cid, title, body) for cid, title, body in pending))
    return [r for r in results if r is not None]


def _reembed_all(
    session: Session,
    *,
    embedder: EmbeddingProvider,
    model: str,
    limit: int,
) -> int:
    rows = session.execute(
        select(KnowledgeChunk.id, KnowledgeChunk.title, KnowledgeChunk.body).limit(limit)
    ).all()
    pending = [(row.id, row.title, row.body) for row in rows]
    embedded = asyncio.run(_embed_batch(embedder, pending))
    for chunk_id, vector in embedded:
        _write_embedding(session, chunk_id=chunk_id, vector=vector, model=model)
    session.commit()
    return len(embedded)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--embed",
        action="store_true",
        help="Write pgvector embeddings (requires --apply and migration 0032)",
    )
    parser.add_argument(
        "--reembed-all",
        action="store_true",
        help=(
            "Re-embed every existing knowledge_chunks row with the currently "
            "configured embedder (requires --apply); does not touch chunk content"
        ),
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help=(
            "Drop chunks of places and routes that are no longer indexed "
            "(unpublished, merged, archived). Use with a --limit that covers everything"
        ),
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=0,
        help=(
            "Cap the local embedding model to this many CPU threads and one "
            "request at a time; use 1 next to a live API"
        ),
    )
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--source", default="internal", help="internal|osm|wikivoyage")
    args = parser.parse_args()
    if not 1 <= args.limit <= 20_000:
        raise SystemExit("limit must be between 1 and 20000")
    if args.embed and not args.apply:
        raise SystemExit("--embed requires --apply")
    if args.reembed_all and not args.apply:
        raise SystemExit("--reembed-all requires --apply")

    concurrency = _EMBED_CONCURRENCY
    if args.threads:
        # On the production host the local model otherwise takes every core,
        # four requests at once, and the API stops answering (2026-10-08).
        # The limits must be set before the model library is first loaded.
        for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
            os.environ[name] = str(args.threads)
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        with contextlib.suppress(ImportError):
            import torch

            torch.set_num_threads(args.threads)
        concurrency = 1

    settings = get_settings()
    embedder = build_embedder(settings)
    if isinstance(embedder, SentenceTransformerEmbeddingProvider):
        # The in-process model is not safe to call from several threads at
        # once: four parallel requests segfaulted on macOS and hung for 48
        # minutes on the production host (2026-10-08). The cap of four is for
        # a remote embedding server only.
        concurrency = 1
    embed_model = settings.rag_embedding_model
    engine = create_engine(settings.database_url_sync)

    if args.reembed_all:
        with Session(engine) as session:
            n = _reembed_all(session, embedder=embedder, model=embed_model, limit=args.limit)
        print(f"[APPLY] re-embedded {n} existing chunk(s) with model={embed_model}")
        return

    counters = {"inserted": 0, "updated": 0, "unchanged": 0, "embedded": 0}
    total = 0
    seen_docs: set[tuple[str, int]] = set()
    pruned = 0
    with Session(engine) as session:
        places = _iter_places(session, limit=args.limit)
        routes = _iter_routes(session, limit=args.limit)
        pending_embed: list[tuple[UUID, str, str]] = []
        for place, locality in places:
            real_description = place.description if _has_real_description(place) else None
            for cand in chunk_place_markdown(
                place_id=str(place.id),
                name=place.name,
                short_description=place.short_description,
                description=real_description,
                locality=locality,
                source=args.source,
            ):
                attrs = {
                    "doc_id": cand.doc_id,
                    "chunk_seq": cand.chunk_seq,
                    "source": cand.source,
                    "license": cand.license_note,
                    "place_id": place.id,
                    "title": cand.title,
                    "region": cand.region,
                    "locality": cand.locality,
                    "lang": "ru",
                    "content_type": cand.content_type,
                    "body": cand.body,
                    "content_hash": content_hash(cand.body),
                    "parsed_at": datetime.now(UTC),
                    "ttl_days": 365,
                    "payload": {"source": cand.source, "place_id": str(place.id)},
                }
                seen_docs.add((cand.doc_id, cand.chunk_seq))
                status, chunk_id = _upsert_chunk(session, attrs=attrs, dry_run=not args.apply)
                counters[status] += 1
                total += 1
                if args.embed and chunk_id is not None:
                    pending_embed.append((chunk_id, cand.title, cand.body))
        for route, locality in routes:
            for cand in chunk_route_markdown(
                route_id=str(route.id),
                name=route.name,
                short_description=route.short_description,
                description="\n\n".join(
                    part for part in (route.description, _route_facts(session, route)) if part
                ),
                locality=locality,
                source=args.source,
            ):
                attrs = {
                    "doc_id": cand.doc_id,
                    "chunk_seq": cand.chunk_seq,
                    "source": cand.source,
                    "license": cand.license_note,
                    "place_id": None,
                    "title": cand.title,
                    "region": cand.region,
                    "locality": cand.locality,
                    "lang": "ru",
                    "content_type": cand.content_type,
                    "body": cand.body,
                    "content_hash": content_hash(cand.body),
                    "parsed_at": datetime.now(UTC),
                    "ttl_days": 365,
                    "payload": {"source": cand.source, "route_id": str(route.id)},
                }
                seen_docs.add((cand.doc_id, cand.chunk_seq))
                status, chunk_id = _upsert_chunk(session, attrs=attrs, dry_run=not args.apply)
                counters[status] += 1
                total += 1
                if args.embed and chunk_id is not None:
                    pending_embed.append((chunk_id, cand.title, cand.body))
        if args.prune:
            pruned = _prune(session, keep=seen_docs, source=args.source, dry_run=not args.apply)
        if args.apply:
            session.flush()
            embedded = asyncio.run(_embed_batch(embedder, pending_embed, concurrency))
            for chunk_id, vector in embedded:
                _write_embedding(session, chunk_id=chunk_id, vector=vector, model=embed_model)
                counters["embedded"] += 1
            session.commit()
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"[{mode}] places+routes scanned={total} "
        f"inserted/updated/unchanged={counters['inserted']}/"
        f"{counters['updated']}/{counters['unchanged']} "
        f"embedded={counters['embedded']} pruned_chunks={pruned}"
    )


if __name__ == "__main__":
    main()

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
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
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
    build_embedder,
)
from tourism_backend.modules.knowledge.infrastructure.models import KnowledgeChunk
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.routes.infrastructure.models import Route

#: Cap concurrent embedding requests against a single home-lab LM Studio
#: instance — cheap to compute but the box only has one GPU.
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
) -> list[tuple[UUID, list[float]]]:
    """Embed (chunk_id, title, body) triples concurrently, capped.

    A chunk whose embed call fails (network hiccup, provider down) is
    skipped rather than aborting the whole batch — it keeps whatever
    embedding it had before (or none), and a later run picks it up again.
    """
    semaphore = asyncio.Semaphore(_EMBED_CONCURRENCY)

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
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--source", default="internal", help="internal|osm|wikivoyage")
    args = parser.parse_args()
    if not 1 <= args.limit <= 20_000:
        raise SystemExit("limit must be between 1 and 20000")
    if args.embed and not args.apply:
        raise SystemExit("--embed requires --apply")
    if args.reembed_all and not args.apply:
        raise SystemExit("--reembed-all requires --apply")

    settings = get_settings()
    embedder = build_embedder(settings)
    embed_model = settings.rag_embedding_model
    engine = create_engine(settings.database_url_sync)

    if args.reembed_all:
        with Session(engine) as session:
            n = _reembed_all(session, embedder=embedder, model=embed_model, limit=args.limit)
        print(f"[APPLY] re-embedded {n} existing chunk(s) with model={embed_model}")
        return

    counters = {"inserted": 0, "updated": 0, "unchanged": 0, "embedded": 0}
    total = 0
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
                description=route.description,
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
                status, chunk_id = _upsert_chunk(session, attrs=attrs, dry_run=not args.apply)
                counters[status] += 1
                total += 1
                if args.embed and chunk_id is not None:
                    pending_embed.append((chunk_id, cand.title, cand.body))
        if args.apply:
            session.flush()
            embedded = asyncio.run(_embed_batch(embedder, pending_embed))
            for chunk_id, vector in embedded:
                _write_embedding(session, chunk_id=chunk_id, vector=vector, model=embed_model)
                counters["embedded"] += 1
            session.commit()
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"[{mode}] places+routes scanned={total} "
        f"inserted/updated/unchanged={counters['inserted']}/"
        f"{counters['updated']}/{counters['unchanged']} "
        f"embedded={counters['embedded']}"
    )


if __name__ == "__main__":
    main()

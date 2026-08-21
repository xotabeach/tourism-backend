#!/usr/bin/env python3
"""Index narrative content (places/routes) into knowledge_chunks for RAG.

Dry-run by default: prints counts that would be inserted/updated, then a
summary. With --apply, chunks are upserted by (doc_id, chunk_seq).

With --embed, also writes pgvector embeddings via the shared HashEmbeddingProvider
(same vectors the retriever uses). Replace with a real model later without
changing the 384-d column.

Examples:
  uv run python scripts/ingest_knowledge.py --limit 300
  uv run python scripts/ingest_knowledge.py --apply --embed
  uv run python scripts/ingest_knowledge.py --apply --limit 1000 --source internal
"""

from __future__ import annotations

import argparse
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
from tourism_backend.modules.knowledge.application.embedder import default_embedder
from tourism_backend.modules.knowledge.infrastructure.models import KnowledgeChunk
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.routes.infrastructure.models import Route

_ = _geo


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
    title: str,
    body: str,
    model: str,
) -> None:
    embedder = default_embedder()
    vector = embedder.embed(f"{title} {body}")
    vec = "[" + ",".join(f"{v:.5f}" for v in vector) + "]"
    session.execute(
        text(
            "UPDATE knowledge_chunks SET embedding = CAST(:vec AS vector), "
            "embedding_model = :model WHERE id = :id"
        ),
        {"vec": vec, "model": model, "id": str(chunk_id)},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--embed",
        action="store_true",
        help="Write pgvector embeddings (requires --apply and migration 0032)",
    )
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--source", default="internal", help="internal|osm|wikivoyage")
    args = parser.parse_args()
    if not 1 <= args.limit <= 20_000:
        raise SystemExit("limit must be between 1 and 20000")
    if args.embed and not args.apply:
        raise SystemExit("--embed requires --apply")

    settings = get_settings()
    engine = create_engine(settings.database_url_sync)
    counters = {"inserted": 0, "updated": 0, "unchanged": 0, "embedded": 0}
    total = 0
    embed_model = settings.rag_embedding_model or default_embedder().model_id
    with Session(engine) as session:
        places = _iter_places(session, limit=args.limit)
        routes = _iter_routes(session, limit=args.limit)
        pending_embed: list[tuple[UUID, str, str]] = []
        for place, locality in places:
            for cand in chunk_place_markdown(
                place_id=str(place.id),
                name=place.name,
                short_description=place.short_description,
                description=place.description,
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
            for chunk_id, title, body in pending_embed:
                _write_embedding(
                    session,
                    chunk_id=chunk_id,
                    title=title,
                    body=body,
                    model=embed_model,
                )
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

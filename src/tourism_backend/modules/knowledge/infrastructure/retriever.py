"""TourismKnowledgeRetriever — hybrid (vector + full-text) retrieval over RAG chunks.

Returns sanitized, allowlisted narrative context. Hard facts (hours, prices,
closures) are NOT retrieved from here — they stay in PostGIS and go through
domain services / tools. Chunks are treated as untrusted DATA (prompt injection
mitigation happens in the caller by framing them as data).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.knowledge.application.embedder import (
    EMBEDDING_DIM,
    HashEmbeddingProvider,
    default_embedder,
)


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    chunk_id: str
    title: str
    body: str
    source: str
    content_type: str
    locality: str | None
    place_id: str | None
    score: float


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    query: str
    top_k: int = 4
    region: str = "crimea"
    locality: str | None = None
    content_type: str | None = None
    min_score: float = 0.0  # 0 disables thresholding


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    chunks: list[RetrievedChunk] = field(default_factory=list)
    total_candidates: int = 0


class TourismKnowledgeRetriever:
    """Runs raw SQL so it doesn't need the optional pgvector python package.

    The ``embedding`` column exists in the DB (created by migration 0032) but
    not on the ORM. We write/read it via raw SQL with the ``<=>`` cosine distance.
    """

    def __init__(
        self,
        *,
        dimension: int = EMBEDDING_DIM,
        embedder: HashEmbeddingProvider | None = None,
    ) -> None:
        self._dimension = dimension
        self._embedder = embedder or default_embedder()

    async def retrieve(
        self,
        session: AsyncSession,
        *,
        request: RetrievalRequest,
    ) -> RetrievalResult:
        query = request.query.strip()
        if not query:
            return RetrievalResult()
        q = query[:400]

        # Build the WHERE clause with safe literals.
        clauses = ["region = :region", "lang = 'ru'"]
        params: dict[str, Any] = {"region": request.region, "topk": request.top_k}
        if request.locality:
            clauses.append("locality ILIKE :locality")
            params["locality"] = f"%{request.locality}%"
        if request.content_type:
            clauses.append("content_type = :ctype")
            params["ctype"] = request.content_type
        where = " AND ".join(clauses)

        # Where is built from fixed fragments only; every user-ish value goes
        # through bind params, so there is no injection surface.
        vec = "[" + ",".join(f"{v:.5f}" for v in self._embedder.embed(q)) + "]"
        vec_sql = "".join(
            (
                "SELECT id, title, body, source, content_type, locality, place_id,"
                " 1 - (embedding <=> CAST(:vec AS vector)) AS score"
                " FROM knowledge_chunks WHERE ",
                where,
                " AND embedding IS NOT NULL"
                " ORDER BY embedding <=> CAST(:vec AS vector) LIMIT :topk",
            )
        )
        rows: Sequence[Any] = []
        try:
            result = await session.execute(
                text(vec_sql),
                {**params, "vec": vec},
            )
            rows = result.all()
        except Exception:  # noqa: BLE001 — fall back to FTS
            rows = []

        if not rows:
            fts_sql = "".join(
                (
                    "SELECT id, title, body, source, content_type, locality, place_id,"
                    " ts_rank_cd("
                    "  to_tsvector('russian', coalesce(title,'') || ' ' ||"
                    "              coalesce(body,'')), plainto_tsquery('russian', :q)"
                    " ) AS score FROM knowledge_chunks"
                    " WHERE ",
                    where,
                    " AND to_tsvector('russian', coalesce(title,'') || ' ' ||"
                    "              coalesce(body,'')) @@ plainto_tsquery('russian', :q)"
                    " ORDER BY score DESC LIMIT :topk",
                )
            )
            result = await session.execute(text(fts_sql), {**params, "q": q})
            rows = result.all()

        chunks, candidates = self._to_chunks(rows, min_score=request.min_score)
        return RetrievalResult(chunks=chunks, total_candidates=candidates)

    def _vec_for(self, q: str) -> list[float]:
        """Compatibility shim used by integration tests / ingest."""
        return self._embedder.embed(q)

    def _to_chunks(
        self,
        rows: Sequence[Any],
        *,
        min_score: float,
    ) -> tuple[list[RetrievedChunk], int]:
        chunks: list[RetrievedChunk] = []
        for row in rows:
            # (id, title, body, source, content_type, locality, place_id, score)
            try:
                chunk_id, title, body, source, ctype, locality, place_id, score = row[:8]
            except ValueError:
                continue
            if min_score > 0 and float(score) < min_score:
                continue
            chunks.append(
                RetrievedChunk(
                    chunk_id=str(chunk_id),
                    title=str(title)[:255],
                    body=str(body)[:1600],
                    source=str(source)[:32],
                    content_type=str(ctype)[:32] or "overview",
                    locality=str(locality) if locality is not None else None,
                    place_id=str(place_id) if place_id is not None else None,
                    score=float(score),
                )
            )
        return chunks, len(rows)

    # -- ingestion helpers used by the CLI ----------------------------------

    async def upsert_embedding(
        self,
        session: AsyncSession,
        *,
        chunk_id: str,
        vector: list[float],
        model: str,
    ) -> None:
        if len(vector) != self._dimension:
            raise ValueError(f"Vector dimension {len(vector)} != {self._dimension}")
        # Use a bind param with an explicit CAST so asyncpg parses it correctly and
        # there is no string interpolation at all.
        vec = "[" + ",".join(f"{v:.5f}" for v in vector) + "]"
        await session.execute(
            text(
                "UPDATE knowledge_chunks SET embedding = CAST(:vec AS vector), "
                "embedding_model = :model WHERE id = :id"
            ),
            {"vec": vec, "model": model, "id": chunk_id},
        )

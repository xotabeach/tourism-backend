"""Bounded MiniLM retrieval. The model scores sources; it never writes answers."""

import asyncio
import logging
import math
from collections.abc import Sequence
from functools import lru_cache
from typing import Protocol
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.knowledge.application.embedder import (
    EMBEDDING_DIM,
    EmbeddingProvider,
    SentenceTransformerEmbeddingProvider,
)
from tourism_backend.modules.support.application.help_visibility import visible_help
from tourism_backend.modules.support.infrastructure.help_models import (
    SupportHelpEmbedding,
    SupportHelpRevision,
)

logger = logging.getLogger(__name__)
MINILM_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MINILM_ALIASES = {MINILM_MODEL, "paraphrase-multilingual-MiniLM-L12-v2"}
SEARCH_PROFILE = "paragraphs-v1"
MAX_INDEX_ARTICLES = 500
MAX_INDEX_PASSAGES = 2000


class HelpIndexProvider(Protocol):
    model_id: str

    async def embed_passages(self, texts: list[str]) -> list[list[float]]: ...


def search_documents(title: str, question: str, body: str) -> list[str]:
    # MiniLM is a short-text/symmetric encoder. Do not silently truncate a
    # whole article into its first 128 tokens. Full bodies remain in FTS.
    # Keep the article's topic on body passages; standalone headings such as
    # "What happens next" are too generic to be independent semantic hits.
    return [f"{title}. {question}"] + [
        f"{title}. {paragraph}" for paragraph in body.split("\n\n") if len(paragraph) > 50
    ]


def normalized_vector(values: Sequence[float]) -> list[float]:
    try:
        valid = len(values) == EMBEDDING_DIM and all(math.isfinite(v) for v in values)
    except (TypeError, OverflowError):
        valid = False
    if not valid:
        raise ValueError("Invalid help embedding dimension or non-finite value")
    norm = math.sqrt(sum(v * v for v in values))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("Invalid help embedding norm")
    return [v / norm for v in values]


class HelpQueryEncoder:
    """Serializes MiniLM work and makes callers queue for it, not skip it.

    The first version let one request through and handed every overlapping
    one back to lexical search. That is the wrong trade: two people typing
    at the same moment got measurably different answer quality for no reason
    they could see, and no way to ask for the better one.

    So the CPU slot is still one at a time — torch on this host has no spare
    cores to interleave — but callers now wait their turn inside a bounded
    budget instead of being dropped. Lexical search remains the fallback for
    the cases where waiting is worse than answering: a queue already too
    long to serve in time, a budget spent, a model that will not load.

    Two invariants worth keeping while editing:
    - the slot is released when the work actually finishes, never when a
      caller stops waiting for it — a cancelled `to_thread` await does not
      stop torch, and releasing early would run two encodes at once;
    - nothing here caches or logs the question.
    """

    def __init__(
        self,
        provider: EmbeddingProvider,
        *,
        timeout_seconds: float = 1.5,
        queue_seconds: float = 6.0,
        max_waiting: int = 12,
    ):
        if provider.model_id not in MINILM_ALIASES:
            raise ValueError("Support semantic search requires multilingual MiniLM, not hash-v1")
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.queue_seconds = queue_seconds
        self.max_waiting = max_waiting
        self._slot = asyncio.Semaphore(1)
        self._waiting = 0

    @property
    def model_id(self) -> str:
        return MINILM_MODEL

    @property
    def waiting(self) -> int:
        """Callers queued or running. Exposed for tests and metrics only."""
        return self._waiting

    async def warm(self) -> None:
        """Loads the weights before anyone is waiting on them.

        Loading is seconds-scale, and it happens inside the first `embed()`.
        Without this the first question after a restart — and every question
        during the load — spends its whole budget waiting for the model and
        falls back to lexical search.
        """
        await self.provider.warm()

    async def encode(self, query: str) -> list[float] | None:
        if self._waiting >= self.max_waiting:
            # A queue this long cannot be served inside anyone's budget;
            # answering now from lexical search beats timing out later.
            logger.warning("support_help_encoder_saturated waiting=%d", self._waiting)
            return None
        self._waiting += 1
        try:
            try:
                await asyncio.wait_for(self._slot.acquire(), self.queue_seconds)
            except TimeoutError:
                logger.warning("support_help_encoder_queue_timeout")
                return None
            try:
                task = asyncio.create_task(self.provider.embed(query))
            except BaseException:
                self._slot.release()
                raise
            # The slot follows the work, not the waiter.
            task.add_done_callback(self._release)
            try:
                values = await asyncio.wait_for(asyncio.shield(task), self.timeout_seconds)
                return normalized_vector(values)
            except Exception as exc:  # noqa: BLE001 — optional retrieval falls back to FTS
                logger.warning("support_help_embedding_unavailable reason=%s", type(exc).__name__)
                return None
        finally:
            self._waiting -= 1

    def _release(self, task: asyncio.Task[list[float]]) -> None:
        if not task.cancelled():
            task.exception()  # Consume late failures after timeout; never log the query.
        self._slot.release()


@lru_cache(maxsize=2)
def help_query_encoder(
    model_id: str,
    timeout_seconds: float,
    queue_seconds: float = 6.0,
    max_waiting: int = 12,
) -> HelpQueryEncoder | None:
    if model_id not in MINILM_ALIASES:
        return None
    # Same provider and process-wide weight cache as tourist RAG; separate
    # data. Cached so every request queues on one shared slot rather than
    # each building its own — which would defeat the whole limit.
    return HelpQueryEncoder(
        SentenceTransformerEmbeddingProvider(model_name=model_id),
        timeout_seconds=timeout_seconds,
        queue_seconds=queue_seconds,
        max_waiting=max_waiting,
    )


async def index_help(
    session: AsyncSession, *, app_version: str, provider: HelpIndexProvider
) -> int:
    if provider.model_id not in MINILM_ALIASES:
        raise ValueError("Only multilingual MiniLM can build the support semantic index")
    # Same lock as publication: no interleaved imports/index replacement.
    await session.execute(text("SELECT pg_advisory_xact_lock(8312056)"))
    rows = list(
        (
            await session.scalars(
                select(SupportHelpRevision)
                .where(*visible_help(app_version))
                .order_by(SupportHelpRevision.article_id)
                .limit(MAX_INDEX_ARTICLES + 1)
            )
        ).all()
    )
    if len(rows) > MAX_INDEX_ARTICLES:
        raise ValueError("Help corpus exceeds the bounded exact-search index")
    changed = 0
    for row in rows:
        key = (row.id, MINILM_MODEL, SEARCH_PROFILE, 0)
        existing = await session.get(SupportHelpEmbedding, key)
        if existing is not None and existing.content_hash == row.content_hash:
            continue
        vectors = await provider.embed_passages(search_documents(row.title, row.question, row.body))
        if not vectors or len(vectors) > 64:
            raise ValueError("Help article exceeds the bounded passage index")
        # Atomic replacement inside the caller's transaction. Never touch
        # another model, profile or revision's embeddings.
        await session.execute(
            delete(SupportHelpEmbedding).where(
                SupportHelpEmbedding.revision_id == row.id,
                SupportHelpEmbedding.model_id == MINILM_MODEL,
                SupportHelpEmbedding.search_profile == SEARCH_PROFILE,
            )
        )
        await session.execute(
            insert(SupportHelpEmbedding).values(
                [
                    {
                        "revision_id": row.id,
                        "model_id": MINILM_MODEL,
                        "search_profile": SEARCH_PROFILE,
                        "passage_index": index,
                        "content_hash": row.content_hash,
                        "embedding": normalized_vector(vector),
                    }
                    for index, vector in enumerate(vectors)
                ]
            )
        )
        changed += 1
    total = await session.scalar(
        select(func.count())
        .select_from(SupportHelpEmbedding)
        .join(SupportHelpRevision, SupportHelpRevision.id == SupportHelpEmbedding.revision_id)
        .where(
            *visible_help(app_version),
            SupportHelpEmbedding.model_id == MINILM_MODEL,
            SupportHelpEmbedding.search_profile == SEARCH_PROFILE,
        )
    )
    if total is not None and total > MAX_INDEX_PASSAGES:
        raise ValueError(
            "Help corpus exceeds the bounded passage index; transaction must roll back"
        )
    return changed


def semantic_ranking(
    query_vector: Sequence[float],
    candidates: Sequence[tuple[UUID, Sequence[float]]],
    *,
    min_score: float,
) -> list[UUID]:
    query = normalized_vector(query_vector)
    by_article: dict[UUID, float] = {}
    for id_, values in candidates:
        try:
            vector = normalized_vector(values)
        except ValueError:
            continue  # Corrupt/stale index data is not a plausible source.
        score = sum(a * b for a, b in zip(query, vector, strict=True))
        if score >= min_score:
            by_article[id_] = max(by_article.get(id_, -1.0), score)
    scores = list(by_article.items())
    scores.sort(key=lambda item: (-item[1], str(item[0])))
    return [id_ for id_, _ in scores[:10]]


def fuse_rankings(lexical: Sequence[UUID], semantic: Sequence[UUID]) -> list[UUID]:
    # Reciprocal rank fusion: FTS rank and cosine are not interchangeable scores.
    scores: dict[UUID, float] = {}
    for ranking in (lexical, semantic):
        for rank, id_ in enumerate(dict.fromkeys(ranking), start=1):
            scores[id_] = scores.get(id_, 0.0) + 1.0 / (60 + rank)
    # Stable ties retain lexical order, then semantic order.
    return sorted(scores, key=lambda id_: -scores[id_])[:3]


async def semantic_help_ids(
    session: AsyncSession,
    *,
    query: str,
    app_version: str,
    encoder: HelpQueryEncoder,
    min_score: float,
) -> list[UUID] | None:
    try:
        # Savepoint keeps a missing/unavailable optional index from poisoning
        # the outer read transaction during a rolling deployment.
        async with session.begin_nested():
            rows = (
                await session.execute(
                    select(SupportHelpRevision.id, SupportHelpEmbedding.embedding)
                    .join(
                        SupportHelpEmbedding,
                        SupportHelpEmbedding.revision_id == SupportHelpRevision.id,
                    )
                    .where(
                        *visible_help(app_version),
                        SupportHelpEmbedding.model_id == encoder.model_id,
                        SupportHelpEmbedding.search_profile == SEARCH_PROFILE,
                        SupportHelpEmbedding.content_hash == SupportHelpRevision.content_hash,
                    )
                    .limit(MAX_INDEX_PASSAGES + 1)
                )
            ).all()
    except SQLAlchemyError as exc:
        logger.warning("support_help_index_unavailable reason=%s", type(exc).__name__)
        return None
    if not rows or len(rows) > MAX_INDEX_PASSAGES:
        return None
    vector = await encoder.encode(query)
    if vector is None:
        return None
    return semantic_ranking(vector, [(r[0], r[1]) for r in rows], min_score=min_score)

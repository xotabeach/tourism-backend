"""RAG retrieval quality eval (articles-mobile-rating-context-rag-backlog-2026-09-01.md §4.9).

Purely retrieval metrics — hit@5 and MRR, no LLM call — against a small,
hand-labeled query set (tests/data/rag_eval.jsonl) so a regression is a
deterministic number, not a vibe. Requires Postgres on localhost:5433 with
pgvector (migration 0032); skips gracefully when the DB is unavailable.

Runs against the hash-v1 bootstrap embedder unconditionally (documents the
known-bad baseline the backlog doc measured by hand) and against the real
local embedder only when the optional 'rag' extra is installed (`uv sync
--extra rag`) — CI's default lint/test jobs don't install it, so that case
is skipped there, not failed.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import close_all_sessions

from tourism_backend.modules.knowledge.application.embedder import (
    EmbeddingProvider,
    HashEmbeddingProvider,
)
from tourism_backend.modules.knowledge.infrastructure.retriever import (
    RetrievalRequest,
    TourismKnowledgeRetriever,
)

try:
    import sentence_transformers as _  # noqa: F401 — presence check only

    from tourism_backend.modules.knowledge.application.embedder import (
        SentenceTransformerEmbeddingProvider,
    )

    _real_embedder: EmbeddingProvider | None = SentenceTransformerEmbeddingProvider(
        model_name="paraphrase-multilingual-MiniLM-L12-v2"
    )
except ImportError:
    _real_embedder = None

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism",
)

_EVAL_SET_PATH = Path(__file__).parent.parent / "data" / "rag_eval.jsonl"
_TOP_K = 5

_INSERT_SQL = """
INSERT INTO knowledge_chunks (
  id, doc_id, chunk_seq, source, title, region, locality, lang, content_type,
  body, content_hash, parsed_at, ttl_days, created_at, updated_at
) VALUES (
  md5(:doc)::uuid, :doc, 0, 'internal', :title, 'crimea', NULL, 'ru',
  'overview', :body, :hash, now(), 365, now(), now())
ON CONFLICT (doc_id, chunk_seq) DO UPDATE SET body = excluded.body
"""

# Same 20 places / 3 routes as data/crimea_seed.json (title, short_description)
# — kept inline rather than loaded from the fixture so this eval doesn't
# silently drift if the seed data changes; doc_ids match its slugs.
_CORPUS: list[tuple[str, str, str]] = [
    (
        "place:swallow-nest",
        "Ласточкино гнездо",
        "Символ Южного берега Крыма. Декоративный замок начала XX века над морем у Гаспры.",
    ),
    (
        "place:vorontsov-palace",
        "Воронцовский дворец",
        "Дворец и парк в Алупке у подножия Ай-Петри. Архитектурный ансамбль XIX века с парком.",
    ),
    (
        "place:livadia-palace",
        "Ливадийский дворец",
        "Белая резиденция Романовых и место Ялтинской конференции.",
    ),
    ("place:massandra-palace", "Массандровский дворец", "Дворец Александра III в Массандре."),
    (
        "place:ai-petri",
        "Ай-Петри",
        "Горный пик с панорамой ЮБК и канатной дорогой на вершину Крымских гор.",
    ),
    ("place:khan-palace", "Ханский дворец", "Резиденция крымских ханов в Бахчисарае."),
    ("place:chufut-kale", "Чуфут-Кале", "Пещерный город над Бахчисараем."),
    ("place:sudak-fortress", "Генуэзская крепость", "Средневековая крепость на горе над Судаком."),
    ("place:novy-svet", "Новый Свет и тропа Голицына", "Бухты и скальная тропа у Нового Света."),
    ("place:kara-dag", "Кара-Даг", "Вулканический массив и заповедник у Коктебеля."),
    (
        "place:feodosia-gallery",
        "Галерея Айвазовского",
        "Национальная картинная галерея в Феодосии.",
    ),
    ("place:tarkhankut", "Мыс Тарханкут", "Скалы, бухты и прозрачная вода западного Крыма."),
    ("place:evpatoria-embankment", "Набережная Евпатории", "Променад и пляжная линия курорта."),
    ("place:khersones", "Херсонес Таврический", "Античный город-музей на берегу Севастополя."),
    ("place:sapun-mountain", "Сапун-гора", "Мемориальный комплекс обороны Севастополя."),
    ("place:balaklava-bay", "Балаклавская бухта", "Узкая бухта и набережная Балаклавы."),
    (
        "place:simferopol-scythian-naples",
        "Неаполь Скифский",
        "Городище и музей под открытым небом в Симферополе.",
    ),
    ("place:marble-cave", "Мраморная пещера", "Карстовая пещера на нижнем плато Чатыр-Дага."),
    ("place:demerdzhi", "Долина привидений", "Скальные фигуры на склоне Демерджи."),
    ("place:alushta-promenade", "Променад Алушты", "Набережная и пляжная зона Алушты."),
    (
        "route:south-coast-classics",
        "Классика Южного берега",
        "Дворцы и символ Крыма за один день у Ялты.",
    ),
    (
        "route:bakhchisaray-heritage",
        "Наследие Бахчисарая",
        "Ханский дворец и пещерный город Чуфут-Кале.",
    ),
    ("route:east-coast-fortresses", "Крепости восточного берега", "Судак, Новый Свет и Кара-Даг."),
]


@dataclass(frozen=True)
class _EvalCase:
    query: str
    expected_doc_ids: frozenset[str]


def _load_eval_set() -> list[_EvalCase]:
    cases = []
    for line in _EVAL_SET_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        cases.append(_EvalCase(row["query"], frozenset(row["expected_doc_ids"])))
    return cases


async def _ensure_table(conn) -> bool:
    try:
        result = await conn.execute(text("SELECT 1 FROM knowledge_chunks LIMIT 1"))
        result.fetchall()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
async def live_db() -> AsyncIterator[object]:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            if not await _ensure_table(conn):
                if os.getenv("CI") or os.getenv("REQUIRE_INTEGRATION_DEPS") == "1":
                    pytest.fail("knowledge_chunks table missing (run migrations)")
                pytest.skip("knowledge_chunks table missing")
    except Exception:  # noqa: BLE001
        if os.getenv("CI") or os.getenv("REQUIRE_INTEGRATION_DEPS") == "1":
            pytest.fail("Postgres unavailable")
        pytest.skip("Postgres for integration tests unavailable")
    yield engine
    await engine.dispose()
    close_all_sessions()


async def _seed_corpus(engine: object, embedder: EmbeddingProvider) -> None:
    async with engine.connect() as conn:  # type: ignore[attr-defined]
        for doc_id, title, body in _CORPUS:
            await conn.execute(
                text(_INSERT_SQL),
                {
                    "doc": doc_id,
                    "title": title,
                    "body": body,
                    "hash": doc_id,
                },
            )
        await conn.commit()

        for doc_id, title, body in _CORPUS:
            vector = await embedder.embed(f"{title} {body}")
            vec_lit = "[" + ",".join(f"{v:.6f}" for v in vector) + "]"
            await conn.execute(
                text(
                    "UPDATE knowledge_chunks SET embedding = CAST(:vec AS vector), "
                    "embedding_model = :model WHERE doc_id = :doc"
                ),
                {"vec": vec_lit, "model": embedder.model_id, "doc": doc_id},
            )
        await conn.commit()


async def _score(
    engine: object, embedder: EmbeddingProvider, cases: list[_EvalCase]
) -> tuple[float, float]:
    retriever = TourismKnowledgeRetriever(embedder=embedder)
    hits = 0
    reciprocal_ranks: list[float] = []
    async with engine.connect() as session:  # type: ignore[attr-defined]
        for case in cases:
            result = await retriever.retrieve(
                session,
                request=RetrievalRequest(query=case.query, top_k=_TOP_K),
            )
            rank = next(
                (i for i, c in enumerate(result.chunks) if c.doc_id in case.expected_doc_ids),
                None,
            )
            if rank is not None:
                hits += 1
                reciprocal_ranks.append(1.0 / (rank + 1))
            else:
                reciprocal_ranks.append(0.0)
    hit_at_k = hits / len(cases)
    mrr = sum(reciprocal_ranks) / len(reciprocal_ranks)
    return hit_at_k, mrr


@pytest.mark.asyncio
async def test_hash_embedder_baseline(live_db: object) -> None:
    """Documents the known-bad baseline — no regression floor on purpose.

    hash-v1 has no semantics (see embedder.py docstring), so this number is
    expected to be low; it exists to have a measured "before" to compare the
    real embedder's "after" against, per §4.5/§4.9 of the backlog doc.
    """
    cases = _load_eval_set()
    await _seed_corpus(live_db, HashEmbeddingProvider())
    hit_at_5, mrr = await _score(live_db, HashEmbeddingProvider(), cases)
    print(f"\n[rag_eval] hash-v1: hit@5={hit_at_5:.2f} mrr={mrr:.2f} n={len(cases)}")


@pytest.mark.skipif(
    _real_embedder is None,
    reason="sentence-transformers not installed (uv sync --extra rag)",
)
@pytest.mark.asyncio
async def test_real_embedder_meets_regression_floor(live_db: object) -> None:
    """The actual quality gate: real semantic retrieval must clear hit@5 >= 0.7.

    0.7 is the backlog doc's own suggested regression floor (§4.9); tighten
    it once a larger, real corpus (not this 23-place smoke set) is ingested.
    """
    assert _real_embedder is not None  # for mypy; the skipif already guards this
    cases = _load_eval_set()
    await _seed_corpus(live_db, _real_embedder)
    hit_at_5, mrr = await _score(live_db, _real_embedder, cases)
    print(
        f"\n[rag_eval] {_real_embedder.model_id}: hit@5={hit_at_5:.2f} mrr={mrr:.2f} n={len(cases)}"
    )
    assert hit_at_5 >= 0.7, (
        f"hit@5={hit_at_5:.2f} fell below the 0.7 regression floor "
        f"(articles-mobile-rating-context-rag-backlog-2026-09-01.md §4.9)"
    )

"""Real PostgreSQL FTS/publication tests; all fixtures roll back, no live publishing."""

import json
import os
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import MetaData, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from tourism_backend.api.deps import get_db_session
from tourism_backend.api.errors import AppError, register_exception_handlers
from tourism_backend.config import Settings
from tourism_backend.modules.support.application.help_content import HelpManifest
from tourism_backend.modules.support.application.help_import import import_help
from tourism_backend.modules.support.application.help_search import read_help, search_help
from tourism_backend.modules.support.application.help_semantic import (
    MINILM_MODEL,
    HelpQueryEncoder,
    index_help,
)
from tourism_backend.modules.support.infrastructure.help_catalog import (
    HelpCatalog,
    load_help_catalog,
)
from tourism_backend.modules.support.infrastructure.help_models import (
    SupportHelpEmbedding,
    SupportHelpRevision,
)
from tourism_backend.modules.support.presentation.router import router

ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism"
)
EVAL = json.loads((ROOT / "tests/data/support_help_search_eval.json").read_text())


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM support_help_revisions LIMIT 1"))
            await conn.execute(text("SELECT 1 FROM support_help_embeddings LIMIT 1"))
    except Exception:  # noqa: BLE001 — required in CI, optional on a DB-less workstation
        await engine.dispose()
        if os.getenv("CI") or os.getenv("REQUIRE_INTEGRATION_DEPS") == "1":
            pytest.fail("Postgres and migration 0057 are required")
        pytest.skip("Postgres with migration 0057 unavailable")
    try:
        async with engine.connect() as conn, conn.begin():
            async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                yield session
            await conn.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
def pack() -> HelpCatalog:
    catalog = load_help_catalog(ROOT / "data/support_help")
    payload = catalog.manifest.model_dump(mode="json")
    # Unique artificial build and explicitly marked approval fixture; never committed.
    payload.update(
        target_app_version=f"99.0.{uuid4().int % 1000000000}",
        status="published",
        release_verified=True,
        approved_by="test-fixture-only",
    )
    return replace(catalog, manifest=HelpManifest.model_validate(payload))


async def publish_fixture(db: AsyncSession, pack: HelpCatalog) -> None:
    await import_help(db, pack, review_until=datetime.now(UTC) + timedelta(days=1))


async def test_help_migration_matches_model(db: AsyncSession) -> None:
    metadata = MetaData()
    SupportHelpRevision.__table__.to_metadata(metadata)
    SupportHelpEmbedding.__table__.to_metadata(metadata)

    def compare(connection):
        context = MigrationContext.configure(
            connection,
            opts={
                "include_name": lambda name, type_, _parents: (
                    type_ != "table"
                    or name in {"support_help_revisions", "support_help_embeddings"}
                )
            },
        )
        return compare_metadata(context, metadata)

    connection = await db.connection()
    assert await connection.run_sync(compare) == []


@pytest.mark.parametrize("case", EVAL, ids=[case["q"] for case in EVAL])
async def test_search_regressions(db: AsyncSession, pack: HelpCatalog, case: dict) -> None:
    await publish_fixture(db, pack)
    result = await search_help(db, query=case["q"], app_version=pack.manifest.target_app_version)
    ids = [item.article_id for item in result.items]
    assert result.available
    assert len(ids) <= 3
    if case["article"] is None:
        assert ids == []
    else:
        assert case["article"] in ids, (case, ids)


async def test_drafts_and_other_builds_are_invisible(db: AsyncSession, pack: HelpCatalog) -> None:
    draft = replace(pack, manifest=pack.manifest.model_copy(update={"status": "draft"}))
    assert await import_help(db, draft) == 15
    assert await import_help(db, draft) == 0
    version = pack.manifest.target_app_version
    assert not (await search_help(db, query="баллы", app_version=version)).available
    with pytest.raises(AppError, match="Статья недоступна"):
        await read_help(db, article_id="points-earn", revision=1, app_version=version)
    await publish_fixture(db, pack)
    assert not (await search_help(db, query="баллы", app_version="0.0.0")).available


async def test_withdrawal_expiry_and_no_implicit_renewal(
    db: AsyncSession, pack: HelpCatalog
) -> None:
    await publish_fixture(db, pack)
    version = pack.manifest.target_app_version
    row = await db.scalar(
        select(SupportHelpRevision).where(
            SupportHelpRevision.app_version == version,
            SupportHelpRevision.article_id == "points-earn",
        )
    )
    assert row is not None
    row.review_until = datetime.now(UTC) - timedelta(seconds=1)
    await db.flush()
    assert await import_help(db, pack, review_until=datetime.now(UTC) + timedelta(days=30)) == 0
    with pytest.raises(AppError, match="Статья недоступна"):
        await read_help(db, article_id=row.article_id, revision=1, app_version=version)
    withdrawn = replace(pack, manifest=pack.manifest.model_copy(update={"status": "withdrawn"}))
    assert await import_help(db, withdrawn) == 15
    assert not (await search_help(db, query="баллы", app_version=version)).available
    with pytest.raises(ValueError, match="withdrawn revision"):
        await publish_fixture(db, pack)


async def test_revision_content_is_immutable_and_replacement_revokes_old_link(
    db: AsyncSession,
    pack: HelpCatalog,
) -> None:
    await publish_fixture(db, pack)
    article = pack.articles[0]
    changed = replace(article, content_hash="a" * 64, plain_text="Новый текст.")
    with pytest.raises(ValueError, match="increment revision"):
        await publish_fixture(db, replace(pack, articles=(changed,)))
    revised = replace(changed, spec=changed.spec.model_copy(update={"revision": 2}))
    await publish_fixture(db, replace(pack, articles=(revised,)))
    version = pack.manifest.target_app_version
    with pytest.raises(AppError, match="Статья недоступна"):
        await read_help(db, article_id=article.spec.id, revision=1, app_version=version)
    result = await read_help(db, article_id=article.spec.id, revision=2, app_version=version)
    assert result.body == "Новый текст."


async def test_http_contract_is_read_only_and_does_not_expose_ticket_data(
    db: AsyncSession,
    pack: HelpCatalog,
) -> None:
    await publish_fixture(db, pack)
    app = FastAPI()
    app.state.settings = Settings(_env_file=None)
    register_exception_handlers(app)
    app.include_router(router, prefix="/api/v1")

    async def session_override() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_db_session] = session_override
    version = pack.manifest.target_app_version
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post(
            "/api/v1/support/help/search", json={"q": "баллы", "app_version": version}
        )
        assert result.status_code == 200
        assert result.headers["cache-control"] == "no-store"
        assert result.json()["method"] == "full_text"
        assert "approved_by" not in result.text
        assert (await client.get("/api/v1/support/tickets")).status_code == 401
        for params in ({"q": "баллы"}, {"q": "x" * 401, "app_version": version}):
            assert (
                await client.post("/api/v1/support/help/search", json=params)
            ).status_code == 422
        injection = await client.post(
            "/api/v1/support/help/search",
            json={
                "q": "'; DROP TABLE support_help_revisions; --",
                "app_version": version,
            },
        )
        assert injection.status_code == 200
        detail = await client.get(
            "/api/v1/support/help/points-earn", params={"revision": 1, "app_version": version}
        )
        assert detail.status_code == 200
        assert detail.json()["body"]
        assert (
            await client.get(
                "/api/v1/support/help/points-earn", params={"revision": 99, "app_version": version}
            )
        ).status_code == 404


class FixtureMiniLM:
    model_id = MINILM_MODEL

    def __init__(self) -> None:
        self.index_calls = 0
        self.query_calls = 0
        self.fail = False

    async def warm(self) -> None:
        pass

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        self.index_calls += 1
        offline = texts[0].startswith("Офлайн-прохождение.")
        return [[float(offline), float(not offline)] + [0.0] * 382]

    async def embed(self, query: str) -> list[float]:
        self.query_calls += 1
        if self.fail:
            raise RuntimeError("fixture unavailable")
        return [1.0] + [0.0] * 383


async def test_hybrid_index_and_query_are_isolated_and_idempotent(
    db: AsyncSession, pack: HelpCatalog
) -> None:
    provider = FixtureMiniLM()
    encoder = HelpQueryEncoder(provider)
    version = pack.manifest.target_app_version
    assert await index_help(db, app_version=version, provider=provider) == 0
    assert provider.index_calls == 0  # Draft/unpublished corpus never reaches the model.
    await publish_fixture(db, pack)
    assert await index_help(db, app_version=version, provider=provider) == 15
    assert await index_help(db, app_version=version, provider=provider) == 0
    assert provider.index_calls == 15
    plain = await search_help(db, query="нет сети в походе", app_version=version)
    assert plain.items == []
    hybrid = await search_help(db, query="нет сети в походе", app_version=version, encoder=encoder)
    assert hybrid.method == "hybrid"
    assert [item.article_id for item in hybrid.items] == ["routes-offline"]
    assert provider.query_calls == 1
    other = await search_help(db, query="нет сети в походе", app_version="0.0.0", encoder=encoder)
    assert not other.available
    assert provider.query_calls == 1


async def test_semantic_index_filters_withdrawal_expiry_and_content_identity(
    db: AsyncSession, pack: HelpCatalog
) -> None:
    await publish_fixture(db, pack)
    provider = FixtureMiniLM()
    version = pack.manifest.target_app_version
    await index_help(db, app_version=version, provider=provider)
    row = await db.scalar(
        select(SupportHelpRevision).where(
            SupportHelpRevision.app_version == version,
            SupportHelpRevision.article_id == "routes-offline",
        )
    )
    assert row is not None
    embedding = await db.scalar(
        select(SupportHelpEmbedding).where(SupportHelpEmbedding.revision_id == row.id)
    )
    assert embedding is not None
    for field, invalid in (
        ("status", "withdrawn"),
        ("review_until", datetime.now(UTC) - timedelta(seconds=1)),
        ("published_at", datetime.now(UTC) + timedelta(days=1)),
    ):
        original = getattr(row, field)
        setattr(row, field, invalid)
        await db.flush()
        result = await search_help(
            db, query="нет сети в походе", app_version=version, encoder=HelpQueryEncoder(provider)
        )
        assert result.items == []
        setattr(row, field, original)
        await db.flush()
    for field, invalid in (
        ("content_hash", "0" * 64),
        ("model_id", "other-384d-model"),
        ("search_profile", "old-profile"),
    ):
        original = getattr(embedding, field)
        setattr(embedding, field, invalid)
        await db.flush()
        result = await search_help(
            db, query="нет сети в походе", app_version=version, encoder=HelpQueryEncoder(provider)
        )
        assert result.items == []
        setattr(embedding, field, original)
        await db.flush()


async def test_optional_model_failure_keeps_lexical_results(db: AsyncSession, pack: HelpCatalog):
    await publish_fixture(db, pack)
    version = pack.manifest.target_app_version
    provider = FixtureMiniLM()
    plain = await search_help(db, query="баллы", app_version=version)
    before_index = await search_help(
        db, query="баллы", app_version=version, encoder=HelpQueryEncoder(provider)
    )
    assert before_index == plain
    assert provider.query_calls == 0
    await index_help(db, app_version=version, provider=provider)
    provider.fail = True
    failed = await search_help(
        db, query="баллы", app_version=version, encoder=HelpQueryEncoder(provider)
    )
    assert failed == plain


async def test_http_feature_flag_controls_semantic_lookup(
    db: AsyncSession, pack: HelpCatalog, monkeypatch
) -> None:
    from tourism_backend.modules.support.presentation import router as module

    await publish_fixture(db, pack)
    version = pack.manifest.target_app_version
    provider = FixtureMiniLM()
    await index_help(db, app_version=version, provider=provider)
    encoder = HelpQueryEncoder(provider)
    monkeypatch.setattr(module, "help_query_encoder", lambda *_args: encoder)
    app = FastAPI()
    app.state.settings = Settings(
        _env_file=None, support_help_semantic_enabled=True, rag_embedding_model=MINILM_MODEL
    )
    app.include_router(router, prefix="/api/v1")

    async def session_override():
        yield db

    app.dependency_overrides[get_db_session] = session_override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        query = {"q": "нет сети в походе", "app_version": version}
        result = await client.post("/api/v1/support/help/search", json=query)
        assert result.status_code == 200
        assert result.json()["method"] == "hybrid"
        assert result.json()["items"][0]["article_id"] == "routes-offline"
        assert "embedding" not in result.text
        app.state.settings.support_help_semantic_enabled = False
        disabled = await client.post("/api/v1/support/help/search", json=query)
        assert disabled.json()["method"] == "full_text"
        assert disabled.json()["items"] == []
        assert provider.query_calls == 1


async def test_revocation_while_query_is_encoding_is_rechecked(
    db: AsyncSession, pack: HelpCatalog
) -> None:
    await publish_fixture(db, pack)
    version = pack.manifest.target_app_version

    class RevokingProvider(FixtureMiniLM):
        async def embed(self, query: str) -> list[float]:
            row = await db.scalar(
                select(SupportHelpRevision).where(
                    SupportHelpRevision.app_version == version,
                    SupportHelpRevision.article_id == "routes-offline",
                )
            )
            assert row is not None
            row.status = "withdrawn"
            await db.flush()
            return await super().embed(query)

    provider = RevokingProvider()
    await index_help(db, app_version=version, provider=provider)
    result = await search_help(
        db, query="нет сети в походе", app_version=version, encoder=HelpQueryEncoder(provider)
    )
    assert result.items == []


@pytest.mark.skipif(os.getenv("RUN_MINILM_EVAL") != "1", reason="Opt-in local CPU model evaluation")
async def test_real_minilm_retrieval_evaluation(db: AsyncSession, pack: HelpCatalog) -> None:
    # Real model; fixtures are synthetic-version publications rolled back by db.
    # Split is selected BEFORE evaluation, not used to silently tune against held-out cases.
    import torch

    from tourism_backend.modules.knowledge.application.embedder import (
        SentenceTransformerEmbeddingProvider,
    )

    torch.set_num_threads(2)  # Local evaluation only; do not change production CPU policy.
    provider = SentenceTransformerEmbeddingProvider(model_name=MINILM_MODEL)
    await provider.warm()
    await publish_fixture(db, pack)
    version = pack.manifest.target_app_version
    assert await index_help(db, app_version=version, provider=provider) == 15
    encoder = HelpQueryEncoder(provider, timeout_seconds=5)
    cases = json.loads((ROOT / "tests/data/support_help_semantic_eval.json").read_text())
    split = os.getenv("HELP_EVAL_SPLIT", "development")
    cases = EVAL if split == "regression" else [case for case in cases if case["split"] == split]
    assert cases
    threshold = float(os.getenv("HELP_EVAL_MIN_SCORE", "0.55"))
    metrics = {
        "full_text": {"hits": 0, "false_matches": 0},
        "hybrid": {"hits": 0, "false_matches": 0},
    }
    misses = []
    latencies = []
    for case in cases:
        for method in metrics:
            started = time.perf_counter()
            result = await search_help(
                db,
                query=case["q"],
                app_version=version,
                encoder=encoder if method == "hybrid" else None,
                semantic_min_score=threshold,
            )
            assert result.method == method
            if method == "hybrid":
                latencies.append(time.perf_counter() - started)
            ids = [item.article_id for item in result.items]
            if case["article"] is None:
                metrics[method]["false_matches"] += bool(ids)
            else:
                metrics[method]["hits"] += case["article"] in ids
            if method == "hybrid" and (
                (case["article"] is None and ids)
                or (case["article"] is not None and case["article"] not in ids)
            ):
                misses.append({"query": case["q"], "expected": case["article"], "got": ids})
    print(
        json.dumps(
            {  # noqa: T201 — explicit opt-in evaluation report, never real user queries
                "split": split,
                "min_score": threshold,
                "cases": len(cases),
                "metrics": metrics,
                "hybrid_max_ms": round(max(latencies) * 1000),
                "misses": misses,
            },
            ensure_ascii=False,
        )
    )
    assert metrics["hybrid"]["hits"] >= metrics["full_text"]["hits"]
    assert metrics["hybrid"]["false_matches"] <= metrics["full_text"]["false_matches"]


async def test_publishing_from_the_admin_retires_what_it_replaces(
    db: AsyncSession, pack: HelpCatalog
) -> None:
    """One published revision per article, version and language.

    `uq_support_help_current` allows exactly one, so publishing revision 2
    has to withdraw revision 1 rather than fail on the index — and the
    operator who published it is what `approved_by` records, instead of
    whatever string a manifest file happened to carry.
    """
    from tourism_backend.modules.support.application.help_publication import (
        DEFAULT_REVIEW_DAYS,
        extend_review,
        publish_revisions,
        withdraw_revisions,
    )

    version = pack.manifest.target_app_version
    now = datetime.now(UTC)
    deadline = now + timedelta(days=DEFAULT_REVIEW_DAYS)

    first = SupportHelpRevision(
        article_id="admin-flow",
        revision=1,
        app_version=version,
        language="ru",
        category="app",
        faq_id="admin-flow",
        title="Заголовок",
        question="Вопрос?",
        body="Достаточно длинный текст инструкции для проверки публикации.",
        content_hash="hash-r1",
        status="draft",
        created_at=now,
        updated_at=now,
    )
    second = SupportHelpRevision(
        article_id="admin-flow",
        revision=2,
        app_version=version,
        language="ru",
        category="app",
        faq_id="admin-flow",
        title="Заголовок",
        question="Вопрос?",
        body="Исправленный текст той же инструкции, вторая ревизия.",
        content_hash="hash-r2",
        status="draft",
        created_at=now,
        updated_at=now,
    )
    db.add_all([first, second])
    await db.flush()

    outcome = await publish_revisions(
        db,
        revision_ids=[first.id],
        approved_by="operator-1",
        review_until=deadline,
        now=now,
    )
    assert outcome.published == 1
    assert first.status == "published"
    assert first.approved_by == "operator-1"
    assert first.review_until == deadline

    # The replacement retires its predecessor instead of colliding with it.
    outcome = await publish_revisions(
        db,
        revision_ids=[second.id],
        approved_by="operator-2",
        review_until=deadline,
        now=now,
    )
    assert (outcome.published, outcome.withdrawn) == (1, 1)
    assert second.status == "published"
    assert first.status == "withdrawn"

    # Publishing an already-published revision is a no-op, not a duplicate.
    outcome = await publish_revisions(
        db,
        revision_ids=[second.id],
        approved_by="operator-2",
        review_until=deadline,
        now=now,
    )
    assert outcome.published == 0
    assert outcome.skipped

    # A withdrawn revision does not come back; a new one is the way forward.
    outcome = await publish_revisions(
        db,
        revision_ids=[first.id],
        approved_by="operator-2",
        review_until=deadline,
        now=now,
    )
    assert outcome.published == 0
    assert first.status == "withdrawn"

    # Extending touches only what is published.
    later = now + timedelta(days=200)
    assert await extend_review(db, revision_ids=[first.id, second.id], review_until=later) == 1
    assert second.review_until == later

    # A deadline in the past would publish something already expired.
    with pytest.raises(ValueError, match="в будущем"):
        await publish_revisions(
            db,
            revision_ids=[second.id],
            approved_by="operator-2",
            review_until=now - timedelta(days=1),
            now=now,
        )

    assert await withdraw_revisions(db, revision_ids=[second.id]) == 1
    assert second.status == "withdrawn"

"""Real PostgreSQL FTS/publication tests; all fixtures roll back, no live publishing."""

import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from tourism_backend.api.deps import get_db_session
from tourism_backend.api.errors import AppError, register_exception_handlers
from tourism_backend.modules.support.application.help_content import HelpManifest
from tourism_backend.modules.support.application.help_import import import_help
from tourism_backend.modules.support.application.help_search import read_help, search_help
from tourism_backend.modules.support.infrastructure.help_catalog import (
    HelpCatalog,
    load_help_catalog,
)
from tourism_backend.modules.support.infrastructure.help_models import SupportHelpRevision
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
    except Exception:  # noqa: BLE001 — required in CI, optional on a DB-less workstation
        await engine.dispose()
        if os.getenv("CI") or os.getenv("REQUIRE_INTEGRATION_DEPS") == "1":
            pytest.fail("Postgres and migration 0056 are required")
        pytest.skip("Postgres with migration 0056 unavailable")
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

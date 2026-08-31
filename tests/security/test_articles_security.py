"""HTTP-level article coverage (Workstream G, step 3).

Focuses on what only the wire can show: who may read an unpublished
article, who may write to someone else's, and that an image upload is
authorized before a single attacker-controlled byte is read.
"""

from __future__ import annotations

import io
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tourism_backend.config import Settings
from tourism_backend.db.redis import create_redis_client
from tourism_backend.main import create_app

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")


async def _deps_available() -> bool:
    try:
        engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        redis = create_redis_client(Settings(redis_url=REDIS_URL))
        await redis.ping()
        await redis.aclose()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
async def live_client() -> AsyncIterator[AsyncClient]:
    if not await _deps_available():
        if os.getenv("CI") or os.getenv("REQUIRE_INTEGRATION_DEPS") == "1":
            pytest.fail("Postgres/Redis required for integration tests are unavailable")
        pytest.skip("Postgres/Redis for integration tests are unavailable")

    settings = Settings(
        app_env="test",
        database_url=DATABASE_URL,
        database_url_sync=DATABASE_URL.replace("+asyncpg", "+psycopg"),
        redis_url=REDIS_URL,
        auth_otp_accept_any=True,
        jwt_signing_key="test-jwt-signing-key-at-least-32-chars!!",
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await app.state.redis.aclose()
    await app.state.engine.dispose()


async def _login(client: AsyncClient, name: str = "Автор") -> str:
    phone = f"+7999{uuid4().int % 10_000_000:07d}"
    req = await client.post("/api/v1/auth/otp/request", json={"display_name": name, "phone": phone})
    assert req.status_code == 204, req.text
    verify = await client.post(
        "/api/v1/auth/otp/verify",
        json={
            "phone": phone,
            "code": "1234",
            "privacy_accepted": True,
            "personal_data_accepted": True,
        },
    )
    assert verify.status_code == 200, verify.text
    return str(verify.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (48, 32), color=(80, 120, 60)).save(buf, format="PNG")
    return buf.getvalue()


async def _create_draft(client: AsyncClient, token: str, **body: object) -> dict:
    payload = {
        "title": "Дорога на Ай-Петри",
        "blocks": [{"block_type": "text", "text_content": "Первый абзац."}],
        **body,
    }
    response = await client.post("/api/v1/articles", json=payload, headers=_auth(token))
    assert response.status_code == 201, response.text
    return dict(response.json())


@pytest.mark.asyncio
async def test_creating_an_article_requires_authentication(live_client: AsyncClient) -> None:
    response = await live_client.post(
        "/api/v1/articles",
        json={"title": "Аноним", "blocks": []},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_public_feed_is_readable_without_an_account(live_client: AsyncClient) -> None:
    response = await live_client.get("/api/v1/articles")
    assert response.status_code == 200
    body = response.json()
    assert "items" in body
    assert all(item["status"] == "published" for item in body["items"])


@pytest.mark.asyncio
async def test_draft_is_visible_to_its_author_and_hidden_from_everyone_else(
    live_client: AsyncClient,
) -> None:
    author = await _login(live_client)
    stranger = await _login(live_client, name="Прохожий")
    draft = await _create_draft(live_client, author)

    mine = await live_client.get(f"/api/v1/articles/{draft['id']}", headers=_auth(author))
    assert mine.status_code == 200
    assert mine.json()["status"] == "draft"

    for headers in ({}, _auth(stranger)):
        response = await live_client.get(f"/api/v1/articles/{draft['id']}", headers=headers)
        assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_another_user_cannot_edit_or_delete_your_article(
    live_client: AsyncClient,
) -> None:
    author = await _login(live_client)
    stranger = await _login(live_client, name="Прохожий")
    draft = await _create_draft(live_client, author)

    edit = await live_client.patch(
        f"/api/v1/articles/{draft['id']}",
        json={"title": "Захвачено", "blocks": []},
        headers=_auth(stranger),
    )
    assert edit.status_code == 404

    removal = await live_client.delete(f"/api/v1/articles/{draft['id']}", headers=_auth(stranger))
    assert removal.status_code == 404

    still_there = await live_client.get(f"/api/v1/articles/{draft['id']}", headers=_auth(author))
    assert still_there.status_code == 200
    assert still_there.json()["title"] == "Дорога на Ай-Петри"


@pytest.mark.asyncio
async def test_submitting_moves_the_article_out_of_the_authors_hands(
    live_client: AsyncClient,
) -> None:
    author = await _login(live_client)
    draft = await _create_draft(live_client, author)

    submitted = await live_client.post(
        f"/api/v1/articles/{draft['id']}/submit", headers=_auth(author)
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "pending_review"

    # An article under moderation must not be editable, or the author
    # could swap the text after a moderator approved it.
    edit = await live_client.patch(
        f"/api/v1/articles/{draft['id']}",
        json={"title": "Подмена", "blocks": []},
        headers=_auth(author),
    )
    assert edit.status_code == 409
    assert edit.json()["error"]["code"] == "article_not_editable"


@pytest.mark.asyncio
async def test_my_articles_lists_drafts_and_requires_auth(live_client: AsyncClient) -> None:
    author = await _login(live_client)
    draft = await _create_draft(live_client, author)

    anonymous = await live_client.get("/api/v1/me/articles")
    assert anonymous.status_code == 401

    mine = await live_client.get("/api/v1/me/articles", headers=_auth(author))
    assert mine.status_code == 200
    assert draft["id"] in {item["id"] for item in mine.json()["items"]}


@pytest.mark.asyncio
async def test_block_image_upload_round_trip(live_client: AsyncClient) -> None:
    author = await _login(live_client)
    draft = await _create_draft(
        live_client,
        author,
        blocks=[
            {"block_type": "text", "text_content": "Вступление"},
            {"block_type": "image"},
        ],
    )
    block_id = draft["blocks"][1]["id"]

    upload = await live_client.post(
        f"/api/v1/articles/{draft['id']}/blocks/{block_id}/image",
        files={"file": ("photo.png", _png_bytes(), "image/png")},
        headers=_auth(author),
    )
    assert upload.status_code == 200, upload.text
    assert upload.json()["image_url"].startswith("/media/articles/")

    detail = await live_client.get(f"/api/v1/articles/{draft['id']}", headers=_auth(author))
    assert detail.status_code == 200
    body = detail.json()
    assert body["blocks"][1]["image_url"] is not None
    # The first image block becomes the feed cover without a second query.
    assert body["cover_image_url"] == body["blocks"][1]["image_url"]

    removal = await live_client.delete(
        f"/api/v1/articles/{draft['id']}/blocks/{block_id}/image", headers=_auth(author)
    )
    assert removal.status_code == 204

    after = await live_client.get(f"/api/v1/articles/{draft['id']}", headers=_auth(author))
    assert after.json()["blocks"][1]["image_url"] is None
    assert after.json()["cover_image_url"] is None


@pytest.mark.asyncio
async def test_stranger_cannot_upload_into_someone_elses_block(
    live_client: AsyncClient,
) -> None:
    author = await _login(live_client)
    stranger = await _login(live_client, name="Прохожий")
    draft = await _create_draft(live_client, author, blocks=[{"block_type": "image"}])
    block_id = draft["blocks"][0]["id"]

    response = await live_client.post(
        f"/api/v1/articles/{draft['id']}/blocks/{block_id}/image",
        files={"file": ("photo.png", _png_bytes(), "image/png")},
        headers=_auth(stranger),
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_text_block_rejects_an_image_upload(live_client: AsyncClient) -> None:
    author = await _login(live_client)
    draft = await _create_draft(live_client, author)
    text_block_id = draft["blocks"][0]["id"]

    response = await live_client.post(
        f"/api/v1/articles/{draft['id']}/blocks/{text_block_id}/image",
        files={"file": ("photo.png", _png_bytes(), "image/png")},
        headers=_auth(author),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "article_block_not_image"


@pytest.mark.asyncio
async def test_article_cannot_link_to_a_route_and_a_place_at_once(
    live_client: AsyncClient,
) -> None:
    author = await _login(live_client)
    response = await live_client.post(
        "/api/v1/articles",
        json={
            "title": "И туда, и сюда",
            "blocks": [],
            "related_route_id": str(uuid4()),
            "related_place_id": str(uuid4()),
        },
        headers=_auth(author),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_article_cannot_point_at_a_route_that_does_not_exist(
    live_client: AsyncClient,
) -> None:
    author = await _login(live_client)
    response = await live_client.post(
        "/api/v1/articles",
        json={
            "title": "Про несуществующий маршрут",
            "blocks": [],
            "related_route_id": str(uuid4()),
        },
        headers=_auth(author),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "article_subject_not_found"

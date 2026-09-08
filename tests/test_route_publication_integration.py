import io
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from tourism_backend.config import Settings
from tourism_backend.db.redis import create_redis_client
from tourism_backend.main import create_app
from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.routes.application import media as route_media
from tourism_backend.modules.routes.infrastructure.models import Route

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")


async def _deps_available() -> bool:
    try:
        engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        await engine.dispose()
        redis = create_redis_client(Settings(redis_url=REDIS_URL))
        await redis.ping()
        await redis.aclose()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
async def publication_context(tmp_path: Path) -> AsyncIterator[tuple[AsyncClient, Any]]:
    if not await _deps_available():
        if os.getenv("CI") or os.getenv("REQUIRE_INTEGRATION_DEPS") == "1":
            pytest.fail("Postgres/Redis required for integration tests are unavailable")
        pytest.skip("Postgres/Redis for integration tests are unavailable")

    route_media._MEDIA_ROOT = tmp_path
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
        yield client, app
    await app.state.redis.aclose()
    await app.state.engine.dispose()


async def _login(client: AsyncClient, phone: str) -> dict[str, Any]:
    requested = await client.post(
        "/api/v1/auth/otp/request",
        json={"display_name": "Автор маршрута", "phone": phone},
    )
    assert requested.status_code == 204, requested.text
    verified = await client.post(
        "/api/v1/auth/otp/verify",
        json={
            "phone": phone,
            "code": "1234",
            "privacy_accepted": True,
            "personal_data_accepted": True,
        },
    )
    assert verified.status_code == 200, verified.text
    return verified.json()


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (48, 32), color=(35, 90, 55)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_user_route_stays_private_until_admin_approval(
    publication_context: tuple[AsyncClient, Any],
) -> None:
    client, app = publication_context
    tokens = await _login(client, f"+7905{uuid4().int % 10_000_000:07d}")
    other = await _login(client, f"+7906{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    places = await client.get(
        "/api/v1/places",
        params={"region_slug": "crimea", "limit": 3},
    )
    assert places.status_code == 200, places.text
    place_ids = [item["id"] for item in places.json()["items"][:2]]
    assert len(place_ids) == 2
    payload = {
        "name": "Пользовательский маршрут",
        "description": "Маршрут для проверки модерации",
        "place_ids": place_ids,
        "filters": ["Природа"],
        "pace": "calm",
        "difficulty": 3,
    }

    saved = await client.post("/api/v1/routes/drafts", headers=headers, json=payload)
    assert saved.status_code == 200, saved.text
    route_id = saved.json()["id"]
    assert saved.json()["publication_status"] == "draft"
    assert (await client.get(f"/api/v1/routes/{route_id}")).status_code == 404

    own_drafts = await client.get("/api/v1/routes/mine", headers=headers)
    assert own_drafts.status_code == 200, own_drafts.text
    assert any(
        item["id"] == route_id and item["publication_status"] == "draft"
        for item in own_drafts.json()["items"]
    )
    foreign_routes = await client.get("/api/v1/routes/mine", headers=other_headers)
    assert all(item["id"] != route_id for item in foreign_routes.json()["items"])

    disposable = await client.post(
        "/api/v1/routes/drafts",
        headers=headers,
        json={**payload, "name": "Удаляемый черновик"},
    )
    disposable_id = disposable.json()["id"]
    foreign_discard = await client.delete(
        f"/api/v1/routes/drafts/{disposable_id}",
        headers=other_headers,
    )
    assert foreign_discard.status_code == 404
    discarded = await client.delete(
        f"/api/v1/routes/drafts/{disposable_id}",
        headers=headers,
    )
    assert discarded.status_code == 204
    after_discard = await client.get("/api/v1/routes/mine", headers=headers)
    assert all(item["id"] != disposable_id for item in after_discard.json()["items"])

    foreign_update = await client.post(
        "/api/v1/routes/drafts",
        headers=other_headers,
        json={**payload, "route_id": route_id},
    )
    assert foreign_update.status_code == 404

    upload = await client.post(
        f"/api/v1/routes/drafts/{route_id}/media",
        headers=headers,
        data={"position": "0"},
        files={"file": ("route.png", _png_bytes(), "image/png")},
    )
    assert upload.status_code == 200, upload.text
    assert upload.json()["public_path"].startswith(f"/media/routes/{route_id}/")
    second_upload = await client.post(
        f"/api/v1/routes/drafts/{route_id}/media",
        headers=headers,
        data={"position": "1"},
        files={"file": ("route-second.png", _png_bytes(), "image/png")},
    )
    assert second_upload.status_code == 200, second_upload.text

    submitted = await client.post(f"/api/v1/routes/{route_id}/submit", headers=headers)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["publication_status"] == "pending_review"
    assert (await client.get(f"/api/v1/routes/{route_id}")).status_code == 404
    own_pending = await client.get("/api/v1/routes/mine", headers=headers)
    assert any(
        item["id"] == route_id and item["publication_status"] == "pending_review"
        for item in own_pending.json()["items"]
    )
    owner_preview = await client.get(
        f"/api/v1/routes/mine/{route_id}",
        headers=headers,
    )
    assert owner_preview.status_code == 200, owner_preview.text
    assert owner_preview.json()["publication_status"] == "pending_review"
    assert len(owner_preview.json()["stops"]) == 2
    assert [item["position"] for item in owner_preview.json()["media"]] == [0, 1]
    assert all(item["kind"] == "image" for item in owner_preview.json()["media"])
    foreign_preview = await client.get(
        f"/api/v1/routes/mine/{route_id}",
        headers=other_headers,
    )
    assert foreign_preview.status_code == 404

    try:
        async with app.state.session_factory() as session:
            route = await session.get(Route, UUID(route_id))
            assert route is not None
            route.publication_status = "published"
            route.visibility = "public"
            route.lifecycle_status = "active"
            await session.commit()
        published = await client.get(f"/api/v1/routes/{route_id}")
        assert published.status_code == 200, published.text
        assert published.json()["publication_status"] == "published"
        assert len(published.json()["media"]) == 2
    finally:
        async with app.state.session_factory() as session:
            await session.execute(
                delete(MediaAttachment).where(
                    MediaAttachment.entity_type == "route",
                    MediaAttachment.entity_id == UUID(route_id),
                )
            )
            route = await session.get(Route, UUID(route_id))
            if route is not None:
                await session.delete(route)
            disposable_route = await session.get(Route, UUID(disposable_id))
            if disposable_route is not None:
                await session.delete(disposable_route)
            await session.commit()


@pytest.mark.asyncio
async def test_withdraw_route_from_pending_and_published(
    publication_context: tuple[AsyncClient, Any],
) -> None:
    client, app = publication_context
    tokens = await _login(client, f"+7907{uuid4().int % 10_000_000:07d}")
    other = await _login(client, f"+7908{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    places = await client.get(
        "/api/v1/places",
        params={"region_slug": "crimea", "limit": 2},
    )
    assert places.status_code == 200, places.text
    place_ids = [item["id"] for item in places.json()["items"][:2]]
    assert len(place_ids) == 2

    saved = await client.post(
        "/api/v1/routes/drafts",
        headers=headers,
        json={
            "name": "Маршрут для отзыва",
            "description": "",
            "place_ids": place_ids,
            "filters": [],
            "pace": "calm",
            "difficulty": 2,
        },
    )
    assert saved.status_code == 200, saved.text
    route_id = saved.json()["id"]

    draft_withdraw = await client.post(
        f"/api/v1/routes/{route_id}/withdraw",
        headers=headers,
    )
    assert draft_withdraw.status_code == 409, draft_withdraw.text

    await client.post(
        f"/api/v1/routes/drafts/{route_id}/media",
        headers=headers,
        data={"position": "0"},
        files={"file": ("route.png", _png_bytes(), "image/png")},
    )
    submitted = await client.post(f"/api/v1/routes/{route_id}/submit", headers=headers)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["publication_status"] == "pending_review"

    try:
        foreign_withdraw = await client.post(
            f"/api/v1/routes/{route_id}/withdraw",
            headers=other_headers,
        )
        assert foreign_withdraw.status_code == 404

        withdrawn = await client.post(
            f"/api/v1/routes/{route_id}/withdraw",
            headers=headers,
        )
        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.json()["publication_status"] == "draft"

        repeat_withdraw = await client.post(
            f"/api/v1/routes/{route_id}/withdraw",
            headers=headers,
        )
        assert repeat_withdraw.status_code == 409

        async with app.state.session_factory() as session:
            route = await session.get(Route, UUID(route_id))
            assert route is not None
            route.publication_status = "published"
            route.visibility = "public"
            route.lifecycle_status = "active"
            await session.commit()

        withdrawn_from_published = await client.post(
            f"/api/v1/routes/{route_id}/withdraw",
            headers=headers,
        )
        assert withdrawn_from_published.status_code == 200, withdrawn_from_published.text
        body = withdrawn_from_published.json()
        assert body["publication_status"] == "draft"
        assert (await client.get(f"/api/v1/routes/{route_id}")).status_code == 404
    finally:
        async with app.state.session_factory() as session:
            await session.execute(
                delete(MediaAttachment).where(
                    MediaAttachment.entity_type == "route",
                    MediaAttachment.entity_id == UUID(route_id),
                )
            )
            route = await session.get(Route, UUID(route_id))
            if route is not None:
                await session.delete(route)
            await session.commit()


@pytest.mark.asyncio
async def test_route_is_editable_from_another_device_and_requeues_when_live(
    publication_context: tuple[AsyncClient, Any],
) -> None:
    """Resuming an edit must not depend on the phone that started it.

    The editor state (pace, filters, difficulty) lives in `accessibility` and
    was never part of the public route payload, so the app could only resume
    from the device still holding the local draft (reported 2026-09-04).
    """
    client, app = publication_context
    tokens = await _login(client, f"+7911{uuid4().int % 10_000_000:07d}")
    other = await _login(client, f"+7912{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    places = await client.get(
        "/api/v1/places",
        params={"region_slug": "crimea", "limit": 3},
    )
    place_ids = [item["id"] for item in places.json()["items"][:2]]
    assert len(place_ids) == 2

    saved = await client.post(
        "/api/v1/routes/drafts",
        headers=headers,
        json={
            "name": "Маршрут с телефона",
            "description": "Начат на одном устройстве",
            "place_ids": place_ids,
            "filters": ["Природа", "С детьми"],
            "pace": "moderate",
            "difficulty": 4,
        },
    )
    assert saved.status_code == 200, saved.text
    route_id = saved.json()["id"]

    # Everything the editor needs comes back, including the fields that only
    # ever lived in `accessibility`.
    editable = await client.get(f"/api/v1/routes/{route_id}/editable", headers=headers)
    assert editable.status_code == 200, editable.text
    body = editable.json()
    assert body["name"] == "Маршрут с телефона"
    assert [item["id"] for item in body["places"]] == place_ids
    # Card data comes along, so the editor redraws stops without a request
    # per place.
    assert all(item["name"] for item in body["places"])
    assert body["filters"] == ["Природа", "С детьми"]
    assert body["pace"] == "moderate"
    assert body["difficulty"] == 4

    # Someone else's route is not editable, and does not leak its content.
    assert (
        await client.get(f"/api/v1/routes/{route_id}/editable", headers=other_headers)
    ).status_code == 404

    async with app.state.session_factory() as session:
        route = await session.get(Route, UUID(route_id))
        assert route is not None
        route.publication_status = "published"
        route.visibility = "public"
        await session.commit()

    # A live route is still editable — but saving puts it back in the queue
    # and out of the catalogue, so the text cannot be swapped after approval.
    resaved = await client.post(
        "/api/v1/routes/drafts",
        headers=headers,
        json={
            "route_id": route_id,
            "name": "Правка с другого телефона",
            "description": "",
            "place_ids": place_ids,
            "filters": [],
            "pace": "calm",
            "difficulty": 2,
        },
    )
    assert resaved.status_code == 200, resaved.text
    assert resaved.json()["publication_status"] == "pending_review"

    # ...and it cannot be pushed through submit a second time.
    resubmit = await client.post(f"/api/v1/routes/{route_id}/submit", headers=headers)
    assert resubmit.status_code == 409, resubmit.text


@pytest.mark.asyncio
async def test_draft_media_survives_a_resave_from_another_device(
    publication_context: tuple[AsyncClient, Any],
) -> None:
    """Photos of a draft reopened elsewhere must not be lost by saving it.

    The editor on the second device holds no files — only the ids the
    `editable` payload gave it — so its save has nothing to re-upload. When
    the only way to change the gallery was "archive everything, then upload",
    that save silently emptied it (reported 2026-09-08).
    """
    client, app = publication_context
    tokens = await _login(client, f"+7909{uuid4().int % 10_000_000:07d}")
    other = await _login(client, f"+7910{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    places = await client.get(
        "/api/v1/places",
        params={"region_slug": "crimea", "limit": 3},
    )
    place_ids = [item["id"] for item in places.json()["items"][:2]]
    saved = await client.post(
        "/api/v1/routes/drafts",
        headers=headers,
        json={
            "name": "Черновик с фотографиями",
            "description": "",
            "place_ids": place_ids,
            "filters": [],
            "pace": "calm",
            "difficulty": 3,
        },
    )
    assert saved.status_code == 200, saved.text
    route_id = saved.json()["id"]

    try:
        media_ids = []
        for position in range(3):
            upload = await client.post(
                f"/api/v1/routes/drafts/{route_id}/media",
                headers=headers,
                data={"position": str(position)},
                files={"file": (f"p{position}.png", _png_bytes(), "image/png")},
            )
            assert upload.status_code == 200, upload.text
            media_ids.append(upload.json()["id"])

        # The editable payload is what a second device gets to work from.
        editable = await client.get(
            f"/api/v1/routes/{route_id}/editable",
            headers=headers,
        )
        assert [item["id"] for item in editable.json()["media"]] == media_ids

        # Keeping every id, reordered, drops nothing and re-numbers positions.
        reordered = [media_ids[2], media_ids[0], media_ids[1]]
        synced = await client.put(
            f"/api/v1/routes/drafts/{route_id}/media",
            headers=headers,
            json={"keep": reordered},
        )
        assert synced.status_code == 204, synced.text
        after = await client.get(
            f"/api/v1/routes/{route_id}/editable",
            headers=headers,
        )
        assert [item["id"] for item in after.json()["media"]] == reordered
        assert [item["position"] for item in after.json()["media"]] == [0, 1, 2]

        # The new first photo becomes the cover, so the card does not keep
        # showing one the author moved to the back.
        async with app.state.session_factory() as session:
            covers = list(
                (
                    await session.scalars(
                        select(MediaAttachment).where(
                            MediaAttachment.entity_type == "route",
                            MediaAttachment.entity_id == UUID(route_id),
                            MediaAttachment.status == "active",
                            MediaAttachment.role == "cover",
                        )
                    )
                ).all()
            )
            assert [item.id for item in covers] == [UUID(reordered[0])]

        # Dropping one archives only that one.
        dropped = await client.put(
            f"/api/v1/routes/drafts/{route_id}/media",
            headers=headers,
            json={"keep": reordered[:2]},
        )
        assert dropped.status_code == 204, dropped.text
        left = await client.get(f"/api/v1/routes/{route_id}/editable", headers=headers)
        assert [item["id"] for item in left.json()["media"]] == reordered[:2]

        # Someone else's draft is not theirs to rearrange.
        foreign = await client.put(
            f"/api/v1/routes/drafts/{route_id}/media",
            headers=other_headers,
            json={"keep": []},
        )
        assert foreign.status_code == 404, foreign.text
        assert (
            len(
                (
                    await client.get(
                        f"/api/v1/routes/{route_id}/editable",
                        headers=headers,
                    )
                ).json()["media"]
            )
            == 2
        )
    finally:
        async with app.state.session_factory() as session:
            await session.execute(
                delete(MediaAttachment).where(
                    MediaAttachment.entity_type == "route",
                    MediaAttachment.entity_id == UUID(route_id),
                )
            )
            await session.commit()


@pytest.mark.asyncio
async def test_resaving_unchanged_points_does_not_route_again(
    publication_context: tuple[AsyncClient, Any],
) -> None:
    """Renaming a route must not wait on a routing call.

    The geometry is recomputed on save because that is when the points can
    change — but most saves change only the title or the description, and
    routing those again is pure waiting for the author (reported 2026-09-08).
    """
    client, app = publication_context
    tokens = await _login(client, f"+7911{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    places = await client.get(
        "/api/v1/places",
        params={"region_slug": "crimea", "limit": 4},
    )
    items = places.json()["items"]
    place_ids = [item["id"] for item in items[:2]]
    payload = {
        "name": "Маршрут",
        "description": "",
        "place_ids": place_ids,
        "filters": [],
        "pace": "calm",
        "difficulty": 3,
    }
    saved = await client.post("/api/v1/routes/drafts", headers=headers, json=payload)
    assert saved.status_code == 200, saved.text
    route_id = saved.json()["id"]

    async def _routing_state() -> tuple[dict[str, Any], bytes | None]:
        async with app.state.session_factory() as session:
            route = await session.get(Route, UUID(route_id))
            assert route is not None
            accessibility = route.accessibility if isinstance(route.accessibility, dict) else {}
            geometry = await session.scalar(
                select(func.ST_AsBinary(Route.geometry)).where(Route.id == UUID(route_id))
            )
            return dict(accessibility.get("routing") or {}), geometry

    routing, geometry = await _routing_state()
    # The stored line remembers which points produced it — that fingerprint
    # is what lets the next save tell "same route" from "moved a point".
    assert routing["place_ids"] == place_ids
    assert geometry is not None

    # A save that only renames keeps the very same line and provenance.
    renamed = await client.post(
        "/api/v1/routes/drafts",
        headers=headers,
        json={**payload, "route_id": route_id, "name": "Другое название"},
    )
    assert renamed.status_code == 200, renamed.text
    after_rename, geometry_after_rename = await _routing_state()
    assert after_rename == routing
    assert geometry_after_rename == geometry

    # Moving a point does route again, and the fingerprint follows.
    if len(items) > 2:
        moved_ids = [place_ids[0], items[2]["id"]]
        moved = await client.post(
            "/api/v1/routes/drafts",
            headers=headers,
            json={**payload, "route_id": route_id, "place_ids": moved_ids},
        )
        assert moved.status_code == 200, moved.text
        after_move, geometry_after_move = await _routing_state()
        assert after_move["place_ids"] == moved_ids
        assert geometry_after_move != geometry

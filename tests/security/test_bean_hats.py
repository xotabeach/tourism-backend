"""Hats for a companion's character (BACKEND-65)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
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


async def _login(client: AsyncClient, phone: str, name: str = "Тестер") -> dict:
    req = await client.post(
        "/api/v1/auth/otp/request",
        json={"display_name": name, "phone": phone},
    )
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
    return verify.json()


async def _user(client: AsyncClient, name: str) -> tuple[str, dict[str, str]]:
    tokens = await _login(client, phone=f"+7906{uuid4().int % 10_000_000:07d}", name=name)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = await client.get("/api/v1/me", headers=headers)
    return me.json()["id"], headers


async def _follow(client: AsyncClient, headers: dict[str, str], user_id: str) -> None:
    response = await client.put(f"/api/v1/users/{user_id}/like", headers=headers)
    assert response.status_code == 204, response.text


def _ids(response) -> list[str]:  # type: ignore[no-untyped-def]
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


async def _friends(
    client: AsyncClient, a: dict[str, str], a_id: str, b: dict[str, str], b_id: str
) -> None:
    await _follow(client, a, b_id)
    await _follow(client, b, a_id)


async def _route_id(client: AsyncClient) -> str:
    response = await client.get("/api/v1/routes", params={"limit": 1})
    assert response.status_code == 200, response.text
    return response.json()["items"][0]["id"]


async def _group(client: AsyncClient, headers: dict[str, str]) -> dict:
    response = await client.post(
        "/api/v1/companion-groups", headers=headers, json={"route_id": await _route_id(client)}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _sql(statement: str, **params: object) -> None:
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text(statement), params)
    await engine.dispose()


def _items(body: dict) -> dict[str, dict]:
    return {item["slug"]: item for item in body["items"]}


@pytest.mark.asyncio
async def test_hats_open_by_tier_and_others_see_the_chosen_one(live_client: AsyncClient) -> None:
    anna, anna_h = await _user(live_client, "Анна")
    vera, vera_h = await _user(live_client, "Вера")
    listed = await live_client.get("/api/v1/me/bean-hats", headers=anna_h)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert len(body["items"]) == 21
    items = _items(body)
    assert [s for s, i in items.items() if i["available"]] == ["beanie", "cap", "headband", "tuft"]
    assert items["panama"]["points_required"] == 300
    assert items["crown"]["tier"] == "premium"
    assert body["selected"] is None

    locked = await live_client.put("/api/v1/me/bean-hat", headers=anna_h, json={"slug": "panama"})
    assert locked.status_code == 409
    assert locked.json()["error"]["code"] == "bean_hat_locked"
    unknown = await live_client.put("/api/v1/me/bean-hat", headers=anna_h, json={"slug": "nope"})
    assert unknown.status_code == 404

    # Points open hats without spending them.
    await _sql("UPDATE users SET travel_points = 900 WHERE id = :id", id=anna)
    opened = _items((await live_client.get("/api/v1/me/bean-hats", headers=anna_h)).json())
    assert opened["panama"]["available"]
    assert opened["straw_hat"]["available"]
    assert not opened["bandana"]["available"]
    chosen = await live_client.put(
        "/api/v1/me/bean-hat", headers=anna_h, json={"slug": "straw_hat"}
    )
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["selected"] == "straw_hat"
    assert chosen.json()["travel_points"] == 900

    # Travel+ opens everything; when it ends the premium hat comes off.
    await _sql("UPDATE users SET travel_plus_active = true WHERE id = :id", id=anna)
    crowned = await live_client.put("/api/v1/me/bean-hat", headers=anna_h, json={"slug": "crown"})
    assert crowned.status_code == 200, crowned.text
    profile = await live_client.get(f"/api/v1/users/{anna}")
    assert profile.json()["bean_hat"] == "crown"

    # Companions see it at the camp.
    await _friends(live_client, anna_h, anna, vera_h, vera)
    group = await _group(live_client, anna_h)
    seen = await live_client.get(f"/api/v1/companion-groups/{group['id']}", headers=anna_h)
    me = next(m for m in seen.json()["members"] if m["user_id"] == anna)
    assert me["bean_hat"] == "crown"

    await _sql("UPDATE users SET travel_plus_active = false WHERE id = :id", id=anna)
    after = (await live_client.get("/api/v1/me/bean-hats", headers=anna_h)).json()
    assert after["selected"] is None
    assert (await live_client.get(f"/api/v1/users/{anna}")).json()["bean_hat"] is None

    back = await live_client.put("/api/v1/me/bean-hat", headers=anna_h, json={"slug": None})
    assert back.status_code == 200


@pytest.mark.asyncio
async def test_build_and_clothes_are_chosen_and_seen_by_companions(
    live_client: AsyncClient,
) -> None:
    anna, anna_h = await _user(live_client, "Анна")
    vera, vera_h = await _user(live_client, "Вера")
    body = (await live_client.get("/api/v1/me/bean-hats", headers=anna_h)).json()
    assert [k["slug"] for k in body["kinds"]] == ["pin", "tall", "small", "round"]
    assert body["kind"] is None
    assert body["outfit"] is None
    outfits = {o["slug"]: o for o in body["outfits"]}
    assert [s for s, o in outfits.items() if o["available"]] == ["hoodie", "sweater"]
    assert outfits["varsity"]["points_required"] == 1500
    assert outfits["vest"]["tier"] == "premium"

    # Every build is free.
    tall = await live_client.put("/api/v1/me/bean-kind", headers=anna_h, json={"slug": "tall"})
    assert tall.status_code == 200, tall.text
    assert tall.json()["kind"] == "tall"
    nope = await live_client.put("/api/v1/me/bean-kind", headers=anna_h, json={"slug": "huge"})
    assert nope.status_code == 404

    locked = await live_client.put(
        "/api/v1/me/bean-outfit", headers=anna_h, json={"slug": "varsity"}
    )
    assert locked.status_code == 409
    assert locked.json()["error"]["code"] == "bean_outfit_locked"
    unknown = await live_client.put("/api/v1/me/bean-outfit", headers=anna_h, json={"slug": "cape"})
    assert unknown.status_code == 404
    hoodie = await live_client.put(
        "/api/v1/me/bean-outfit", headers=anna_h, json={"slug": "hoodie"}
    )
    assert hoodie.status_code == 200, hoodie.text
    assert hoodie.json()["outfit"] == "hoodie"

    profile = (await live_client.get(f"/api/v1/users/{anna}")).json()
    assert (profile["bean_kind"], profile["bean_outfit"]) == ("tall", "hoodie")
    me = (await live_client.get("/api/v1/me", headers=anna_h)).json()
    assert (me["bean_kind"], me["bean_outfit"]) == ("tall", "hoodie")

    await _friends(live_client, anna_h, anna, vera_h, vera)
    group = await _group(live_client, anna_h)
    seen = await live_client.get(f"/api/v1/companion-groups/{group['id']}", headers=anna_h)
    member = next(m for m in seen.json()["members"] if m["user_id"] == anna)
    assert (member["bean_kind"], member["bean_outfit"]) == ("tall", "hoodie")

    off = await live_client.put("/api/v1/me/bean-outfit", headers=anna_h, json={"slug": None})
    assert off.json()["outfit"] is None
    given = await live_client.put("/api/v1/me/bean-kind", headers=anna_h, json={"slug": None})
    assert given.json()["kind"] is None


@pytest.mark.asyncio
async def test_an_expert_has_every_hat_and_every_outfit(live_client: AsyncClient) -> None:
    anna, anna_h = await _user(live_client, "Анна")
    await _sql("UPDATE users SET is_expert = true WHERE id = :id", id=anna)
    body = (await live_client.get("/api/v1/me/bean-hats", headers=anna_h)).json()
    assert body["is_expert"] is True
    assert all(item["available"] for item in body["items"])
    assert all(outfit["available"] for outfit in body["outfits"])
    assert {i["unlocked_by"] for i in body["items"]} == {"free", "expert"}

    crown = await live_client.put("/api/v1/me/bean-hat", headers=anna_h, json={"slug": "crown"})
    assert crown.status_code == 200, crown.text
    vest = await live_client.put("/api/v1/me/bean-outfit", headers=anna_h, json={"slug": "vest"})
    assert vest.status_code == 200, vest.text
    profile = (await live_client.get(f"/api/v1/users/{anna}")).json()
    assert (profile["bean_hat"], profile["bean_outfit"]) == ("crown", "vest")

    # When the rank is taken away, what it opened comes off.
    await _sql("UPDATE users SET is_expert = false WHERE id = :id", id=anna)
    profile = (await live_client.get(f"/api/v1/users/{anna}")).json()
    assert (profile["bean_hat"], profile["bean_outfit"]) == (None, None)

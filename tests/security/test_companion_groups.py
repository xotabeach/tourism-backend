"""Groups of companions and their invitations (BACKEND-59, spec 11b)."""

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
from tourism_backend.modules.companions.application.groups import CODE_ATTEMPTS_PER_HOUR

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


@pytest.mark.asyncio
async def test_invite_accept_and_what_each_side_sees(live_client: AsyncClient) -> None:
    anna, anna_h = await _user(live_client, "Анна")
    vera, vera_h = await _user(live_client, "Вера")
    boris, boris_h = await _user(live_client, "Борис")
    await _friends(live_client, anna_h, anna, vera_h, vera)
    group = await _group(live_client, anna_h)
    assert group["status"] == "gathering"
    assert group["is_organizer"] is True
    assert group["max_members"] == 8
    assert len(group["join_code"]) == 6

    # Not a mutual follower.
    refused = await live_client.post(
        f"/api/v1/companion-groups/{group['id']}/invites", headers=anna_h, json={"user_id": boris}
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "invite_not_mutual"
    assert boris_h

    invited = await live_client.post(
        f"/api/v1/companion-groups/{group['id']}/invites", headers=anna_h, json={"user_id": vera}
    )
    assert invited.status_code == 200, invited.text

    # The invitee sees the invitation, the organizer, but not the code.
    mine = await live_client.get("/api/v1/me/companion-groups", headers=vera_h)
    [item] = mine.json()["items"]
    assert item["my_state"] == "invited"
    assert item["join_code"] is None
    assert [m["user_id"] for m in item["members"]] == [anna]
    inbox = await live_client.get("/api/v1/me/notifications", headers=vera_h)
    assert inbox.status_code == 200, inbox.text
    assert any(n["kind"] == "companion_invite" for n in inbox.json()["items"])

    accepted = await live_client.post(
        f"/api/v1/companion-groups/{group['id']}/accept", headers=vera_h
    )
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["my_state"] == "joined"
    assert body["join_code"] == group["join_code"]
    assert {m["user_id"] for m in body["members"]} == {anna, vera}

    # Strangers do not see the group at all.
    hidden = await live_client.get(f"/api/v1/companion-groups/{group['id']}", headers=boris_h)
    assert hidden.status_code == 404


@pytest.mark.asyncio
async def test_a_declined_invitation_is_not_sent_again(live_client: AsyncClient) -> None:
    anna, anna_h = await _user(live_client, "Анна")
    vera, vera_h = await _user(live_client, "Вера")
    await _friends(live_client, anna_h, anna, vera_h, vera)
    group = await _group(live_client, anna_h)
    url = f"/api/v1/companion-groups/{group['id']}"
    await live_client.post(f"{url}/invites", headers=anna_h, json={"user_id": vera})
    declined = await live_client.post(f"{url}/decline", headers=vera_h)
    assert declined.status_code == 200, declined.text
    again = await live_client.post(f"{url}/invites", headers=anna_h, json={"user_id": vera})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "companion_invite_declined"


@pytest.mark.asyncio
async def test_a_code_asks_the_organizer_who_decides(live_client: AsyncClient) -> None:
    anna, anna_h = await _user(live_client, "Анна")
    gleb, gleb_h = await _user(live_client, "Глеб")
    group = await _group(live_client, anna_h)

    wrong = await live_client.post(
        "/api/v1/companion-groups/join", headers=gleb_h, json={"code": "ZZZZZZ"}
    )
    assert wrong.status_code == 404
    asked = await live_client.post(
        "/api/v1/companion-groups/join",
        headers=gleb_h,
        json={"code": group["join_code"].lower()},
    )
    assert asked.status_code == 200, asked.text
    assert asked.json()["my_state"] == "requested"
    inbox = await live_client.get("/api/v1/me/notifications", headers=anna_h)
    assert any(n["kind"] == "companion_join_request" for n in inbox.json()["items"])

    seen = await live_client.get(f"/api/v1/companion-groups/{group['id']}", headers=anna_h)
    assert any(m["user_id"] == gleb and m["state"] == "requested" for m in seen.json()["members"])
    approved = await live_client.post(
        f"/api/v1/companion-groups/{group['id']}/requests/{gleb}/approve", headers=anna_h
    )
    assert approved.status_code == 200, approved.text
    assert any(m["user_id"] == gleb and m["state"] == "joined" for m in approved.json()["members"])


@pytest.mark.asyncio
async def test_code_attempts_are_limited(live_client: AsyncClient) -> None:
    _gleb, gleb_h = await _user(live_client, "Глеб")
    statuses = [
        (
            await live_client.post(
                "/api/v1/companion-groups/join", headers=gleb_h, json={"code": "ZZZZZZ"}
            )
        ).status_code
        for _ in range(CODE_ATTEMPTS_PER_HOUR + 1)
    ]
    assert statuses[:-1] == [404] * CODE_ATTEMPTS_PER_HOUR
    assert statuses[-1] == 429


@pytest.mark.asyncio
async def test_a_block_closes_invites_and_codes(live_client: AsyncClient) -> None:
    anna, anna_h = await _user(live_client, "Анна")
    vera, vera_h = await _user(live_client, "Вера")
    await _friends(live_client, anna_h, anna, vera_h, vera)
    group = await _group(live_client, anna_h)
    await live_client.put(f"/api/v1/users/{anna}/block", headers=vera_h)
    invite = await live_client.post(
        f"/api/v1/companion-groups/{group['id']}/invites", headers=anna_h, json={"user_id": vera}
    )
    assert invite.status_code == 409
    assert invite.json()["error"]["code"] == "invite_blocked"
    code = await live_client.post(
        "/api/v1/companion-groups/join", headers=vera_h, json={"code": group["join_code"]}
    )
    assert code.status_code == 404


@pytest.mark.asyncio
async def test_one_live_group_at_a_time_and_only_published_routes(
    live_client: AsyncClient,
) -> None:
    _anna, anna_h = await _user(live_client, "Анна")
    await _group(live_client, anna_h)
    second = await live_client.post(
        "/api/v1/companion-groups", headers=anna_h, json={"route_id": await _route_id(live_client)}
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "companion_already_in_group"

    _boris, boris_h = await _user(live_client, "Борис")
    missing = await live_client.post(
        "/api/v1/companion-groups", headers=boris_h, json={"route_id": str(uuid4())}
    )
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "companion_route_not_public"


@pytest.mark.asyncio
async def test_cancel_leave_and_remove(live_client: AsyncClient) -> None:
    anna, anna_h = await _user(live_client, "Анна")
    vera, vera_h = await _user(live_client, "Вера")
    gleb, gleb_h = await _user(live_client, "Глеб")
    await _friends(live_client, anna_h, anna, vera_h, vera)
    group = await _group(live_client, anna_h)
    url = f"/api/v1/companion-groups/{group['id']}"
    await live_client.post(f"{url}/invites", headers=anna_h, json={"user_id": vera})
    await live_client.post(f"{url}/accept", headers=vera_h)
    await live_client.post(
        "/api/v1/companion-groups/join", headers=gleb_h, json={"code": group["join_code"]}
    )
    await live_client.post(f"{url}/requests/{gleb}/approve", headers=anna_h)

    # A member cannot remove anyone else, only leave.
    assert (await live_client.delete(f"{url}/members/{gleb}", headers=vera_h)).status_code == 403
    left = await live_client.delete(f"{url}/members/{vera}", headers=vera_h)
    assert left.status_code == 204
    removed = await live_client.delete(f"{url}/members/{gleb}", headers=anna_h)
    assert removed.status_code == 200, removed.text
    assert {m["user_id"] for m in removed.json()["members"]} == {anna}
    assert (await live_client.get(url, headers=gleb_h)).status_code == 404

    # The organizer cancels rather than leaves.
    assert (await live_client.delete(f"{url}/members/{anna}", headers=anna_h)).status_code == 409
    cancelled = await live_client.post(f"{url}/cancel", headers=anna_h)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    # And may start another group.
    await _group(live_client, anna_h)


@pytest.mark.asyncio
async def test_a_route_taken_off_the_catalogue_cancels_the_group(
    live_client: AsyncClient,
) -> None:
    anna, anna_h = await _user(live_client, "Анна")
    vera, vera_h = await _user(live_client, "Вера")
    await _friends(live_client, anna_h, anna, vera_h, vera)
    group = await _group(live_client, anna_h)
    url = f"/api/v1/companion-groups/{group['id']}"
    await live_client.post(f"{url}/invites", headers=anna_h, json={"user_id": vera})
    await live_client.post(f"{url}/accept", headers=vera_h)
    try:
        await _sql(
            "UPDATE routes SET publication_status = 'draft' WHERE id = :id", id=group["route_id"]
        )
        seen = await live_client.get(url, headers=vera_h)
        assert seen.json()["status"] == "cancelled"
        assert seen.json()["cancel_reason"] == "route_unavailable"
        inbox = await live_client.get("/api/v1/me/notifications", headers=vera_h)
        assert any(n["kind"] == "companion_group_cancelled" for n in inbox.json()["items"])
    finally:
        await _sql(
            "UPDATE routes SET publication_status = 'published' WHERE id = :id",
            id=group["route_id"],
        )


@pytest.mark.asyncio
async def test_a_complaint_declines_and_lands_in_its_own_queue(live_client: AsyncClient) -> None:
    anna, anna_h = await _user(live_client, "Анна")
    vera, vera_h = await _user(live_client, "Вера")
    await _friends(live_client, anna_h, anna, vera_h, vera)
    group = await _group(live_client, anna_h)
    url = f"/api/v1/companion-groups/{group['id']}"
    await live_client.post(f"{url}/invites", headers=anna_h, json={"user_id": vera})
    report = await live_client.post(
        f"{url}/report", headers=vera_h, json={"reason": "spam", "comment": "Зовёт каждый день"}
    )
    assert report.status_code == 204, report.text
    mine = await live_client.get("/api/v1/me/companion-groups", headers=vera_h)
    assert mine.json()["items"] == []
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as conn:
        count = await conn.scalar(
            text(
                "SELECT count(*) FROM content_reports WHERE target_type = 'companion_group' "
                "AND target_id = :id"
            ),
            {"id": group["id"]},
        )
    await engine.dispose()
    assert count == 1

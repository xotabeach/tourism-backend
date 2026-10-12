"""Co-authors of a user route, the designer's scheme (BACKEND-62, spec 11c D7)."""

from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import delete

from test_route_publication_integration import (  # noqa: F401
    _login,
    _png_bytes,
    publication_context,
)
from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.routes.infrastructure.models import Route

API = "/api/v1"


async def _person(client: AsyncClient) -> tuple[str, dict[str, str]]:
    tokens = await _login(client, f"+7923{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = await client.get(f"{API}/me", headers=headers)
    return me.json()["id"], headers


async def _friends(client: AsyncClient, *people: tuple[str, dict[str, str]]) -> None:
    for a_id, a_h in people:
        for b_id, _b_h in people:
            if a_id != b_id:
                assert (
                    await client.put(f"{API}/users/{b_id}/like", headers=a_h)
                ).status_code == 204


async def _places(client: AsyncClient, count: int) -> list[str]:
    places = await client.get(f"{API}/places", params={"region_slug": "crimea", "limit": count})
    return [item["id"] for item in places.json()["items"][:count]]


def _draft(place_ids: list[str], route_id: str | None = None, name: str = "Вдвоём") -> dict:
    return {
        **({"route_id": route_id} if route_id else {}),
        "name": name,
        "description": "Маршрут в соавторстве",
        "place_ids": place_ids,
        "filters": ["Природа"],
        "pace": "calm",
        "difficulty": 3,
    }


async def _route(client: AsyncClient, headers: dict[str, str], place_ids: list[str]) -> str:
    saved = await client.post(f"{API}/routes/drafts", headers=headers, json=_draft(place_ids))
    assert saved.status_code == 200, saved.text
    route_id: str = saved.json()["id"]
    upload = await client.post(
        f"{API}/routes/drafts/{route_id}/media",
        headers=headers,
        data={"position": "0"},
        files={"file": ("route.png", _png_bytes(), "image/png")},
    )
    assert upload.status_code == 200, upload.text
    return route_id


async def _cleanup(app: Any, route_id: str) -> None:
    async with app.state.session_factory() as session:
        await session.execute(
            delete(MediaAttachment).where(
                MediaAttachment.entity_type == "route",
                MediaAttachment.entity_id == UUID(route_id),
            )
        )
        await session.execute(delete(Route).where(Route.id == UUID(route_id)))
        await session.commit()


@pytest.mark.asyncio
async def test_the_route_goes_to_review_when_every_author_agreed(
    publication_context: tuple[AsyncClient, Any],  # noqa: F811
) -> None:
    client, app = publication_context
    owner = await _person(client)
    andrey = await _person(client)
    await _friends(client, owner, andrey)
    place_ids = await _places(client, 3)
    route_id = await _route(client, owner[1], place_ids[:2])
    try:
        chosen = await client.put(
            f"{API}/routes/{route_id}/coauthors",
            headers=owner[1],
            json={"user_ids": [andrey[0]]},
        )
        assert chosen.status_code == 200, chosen.text
        assert [(c["user_id"], c["state"]) for c in chosen.json()] == [(andrey[0], "invited")]
        inbox = await client.get(f"{API}/me/notifications", headers=andrey[1])
        offer = next(n for n in inbox.json()["items"] if n["kind"] == "route_coauthor_invite")
        assert offer["target_id"] == route_id
        assert "предлагает вам соавторство" in offer["title"]

        # The owner publishes first: the route waits for Andrey.
        first = await client.post(f"{API}/routes/{route_id}/submit", headers=owner[1])
        assert first.status_code == 200, first.text
        assert first.json()["awaiting_coauthors"] is True
        assert first.json()["publication_status"] == "draft"

        # Andrey opens the same page, sees the route and the authors.
        page = await client.get(f"{API}/routes/{route_id}/editable", headers=andrey[1])
        assert page.status_code == 200, page.text
        assert page.json()["my_role"] == "coauthor"
        assert page.json()["owner_consented"] is True
        mine = await client.get(f"{API}/routes/mine", headers=andrey[1])
        assert route_id in [item["id"] for item in mine.json()["items"]]

        second = await client.post(f"{API}/routes/{route_id}/submit", headers=andrey[1])
        assert second.status_code == 200, second.text
        assert second.json()["awaiting_coauthors"] is False
        assert second.json()["publication_status"] == "pending_review"
    finally:
        await _cleanup(app, route_id)


@pytest.mark.asyncio
async def test_an_edit_after_a_consent_asks_everyone_again(
    publication_context: tuple[AsyncClient, Any],  # noqa: F811
) -> None:
    client, app = publication_context
    owner = await _person(client)
    andrey = await _person(client)
    await _friends(client, owner, andrey)
    place_ids = await _places(client, 3)
    route_id = await _route(client, owner[1], place_ids[:2])
    try:
        await client.put(
            f"{API}/routes/{route_id}/coauthors", headers=owner[1], json={"user_ids": [andrey[0]]}
        )
        await client.post(f"{API}/routes/{route_id}/submit", headers=owner[1])
        # Andrey changes the route instead of agreeing to it.
        edited = await client.post(
            f"{API}/routes/drafts",
            headers=andrey[1],
            json=_draft(place_ids, route_id=route_id, name="Вдвоём, правка Андрея"),
        )
        assert edited.status_code == 200, edited.text
        page = (await client.get(f"{API}/routes/{route_id}/editable", headers=owner[1])).json()
        assert page["name"] == "Вдвоём, правка Андрея"
        assert page["owner_consented"] is False

        agreed = await client.post(f"{API}/routes/{route_id}/submit", headers=andrey[1])
        assert agreed.json()["awaiting_coauthors"] is True
        sent = await client.post(f"{API}/routes/{route_id}/submit", headers=owner[1])
        assert sent.json()["publication_status"] == "pending_review"
    finally:
        await _cleanup(app, route_id)


@pytest.mark.asyncio
async def test_rules_for_who_can_be_a_coauthor_and_what_they_may_do(
    publication_context: tuple[AsyncClient, Any],  # noqa: F811
) -> None:
    client, app = publication_context
    owner = await _person(client)
    andrey = await _person(client)
    stranger = await _person(client)
    friends = [await _person(client) for _ in range(3)]
    await _friends(client, owner, andrey, *friends)
    place_ids = await _places(client, 2)
    route_id = await _route(client, owner[1], place_ids)
    url = f"{API}/routes/{route_id}/coauthors"
    try:
        not_mutual = await client.put(url, headers=owner[1], json={"user_ids": [stranger[0]]})
        assert not_mutual.status_code == 409
        assert not_mutual.json()["error"]["code"] == "invite_not_mutual"
        too_many = await client.put(
            url, headers=owner[1], json={"user_ids": [andrey[0]] + [f[0] for f in friends]}
        )
        assert too_many.status_code == 409
        assert too_many.json()["error"]["code"] == "route_coauthors_limit"

        await client.put(url, headers=owner[1], json={"user_ids": [andrey[0]]})
        # Only the owner chooses co-authors, deletes and withdraws.
        assert (await client.put(url, headers=andrey[1], json={"user_ids": []})).status_code == 404
        assert (
            await client.delete(f"{API}/routes/drafts/{route_id}", headers=andrey[1])
        ).status_code == 404
        # A stranger cannot open the editor.
        assert (
            await client.get(f"{API}/routes/{route_id}/editable", headers=stranger[1])
        ).status_code == 404

        # Andrey declines: the owner hears of it and publishes alone.
        left = await client.delete(f"{API}/routes/{route_id}/coauthorship", headers=andrey[1])
        assert left.status_code == 204
        inbox = await client.get(f"{API}/me/notifications", headers=owner[1])
        assert any(n["kind"] == "route_coauthor_declined" for n in inbox.json()["items"])
        assert (
            await client.get(f"{API}/routes/{route_id}/editable", headers=andrey[1])
        ).status_code == 404
        alone = await client.post(f"{API}/routes/{route_id}/submit", headers=owner[1])
        assert alone.json()["publication_status"] == "pending_review"
    finally:
        await _cleanup(app, route_id)


@pytest.mark.asyncio
async def test_a_published_route_names_its_coauthors(
    publication_context: tuple[AsyncClient, Any],  # noqa: F811
) -> None:
    client, app = publication_context
    owner = await _person(client)
    andrey = await _person(client)
    await _friends(client, owner, andrey)
    place_ids = await _places(client, 2)
    route_id = await _route(client, owner[1], place_ids)
    try:
        await client.put(
            f"{API}/routes/{route_id}/coauthors", headers=owner[1], json={"user_ids": [andrey[0]]}
        )
        await client.post(f"{API}/routes/{route_id}/submit", headers=owner[1])
        await client.post(f"{API}/routes/{route_id}/submit", headers=andrey[1])
        async with app.state.session_factory() as session:
            route = await session.get(Route, UUID(route_id))
            assert route is not None
            route.publication_status = "published"
            route.visibility = "public"
            route.lifecycle_status = "active"
            await session.commit()

        detail = await client.get(f"{API}/routes/{route_id}")
        assert detail.status_code == 200, detail.text
        assert [c["user_id"] for c in detail.json()["coauthors"]] == [andrey[0]]
        profile = await client.get(f"{API}/users/{andrey[0]}/routes")
        assert route_id in [item["id"] for item in profile.json()["items"]]
        # A co-author cannot report their own route.
        report = await client.post(
            f"{API}/reports",
            headers=andrey[1],
            json={"target_type": "route", "target_id": route_id, "reason": "spam"},
        )
        assert report.status_code == 400, report.text
    finally:
        await _cleanup(app, route_id)

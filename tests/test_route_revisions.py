"""Versions of a published route (BACKEND-38, spec 15 D5, D6, D17)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select

from test_route_publication_integration import (  # noqa: F401
    _login,
    _png_bytes,
    publication_context,
)
from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.notifications.application import service as notifications_service
from tourism_backend.modules.routes.application.route_revisions import (
    VersionFacts,
    apply_revision,
    diff_versions,
    revision_diff,
    revision_of,
)
from tourism_backend.modules.routes.infrastructure.models import Route, RouteReview


async def _published_route(
    client: AsyncClient, app: Any, headers: dict[str, str], place_ids: list[str]
) -> str:
    saved = await client.post(
        "/api/v1/routes/drafts",
        headers=headers,
        json={
            "name": "Опубликованный маршрут",
            "description": "Первая версия",
            "place_ids": place_ids,
            "filters": ["Природа"],
            "pace": "calm",
            "difficulty": 3,
        },
    )
    assert saved.status_code == 200, saved.text
    route_id: str = saved.json()["id"]
    upload = await client.post(
        f"/api/v1/routes/drafts/{route_id}/media",
        headers=headers,
        data={"position": "0"},
        files={"file": ("route.png", _png_bytes(), "image/png")},
    )
    assert upload.status_code == 200, upload.text
    async with app.state.session_factory() as session:
        route = await session.get(Route, UUID(route_id))
        assert route is not None
        route.publication_status = "published"
        route.visibility = "public"
        route.lifecycle_status = "active"
        await session.commit()
    return route_id


async def _cleanup(app: Any, route_id: str) -> None:
    async with app.state.session_factory() as session:
        ids = [UUID(route_id)]
        ids += list(
            await session.scalars(
                select(Route.id).where(Route.revision_of_route_id == UUID(route_id))
            )
        )
        await session.execute(
            delete(MediaAttachment).where(
                MediaAttachment.entity_type == "route", MediaAttachment.entity_id.in_(ids)
            )
        )
        await session.execute(delete(Route).where(Route.id == UUID(route_id)))
        await session.commit()


async def _places(client: AsyncClient, count: int) -> list[str]:
    places = await client.get("/api/v1/places", params={"region_slug": "crimea", "limit": count})
    ids = [item["id"] for item in places.json()["items"][:count]]
    assert len(ids) == count
    return ids


def _edit(route_id: str, place_ids: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "route_id": route_id,
        "name": "Обновлённый маршрут",
        "description": "Вторая версия",
        "place_ids": place_ids,
        "filters": ["Природа"],
        "pace": "calm",
        "difficulty": 3,
        **extra,
    }


@pytest.mark.asyncio
async def test_an_edit_waits_beside_the_published_route_until_approved(
    publication_context: tuple[AsyncClient, Any],  # noqa: F811
) -> None:
    client, app = publication_context
    tokens = await _login(client, f"+7921{uuid4().int % 10_000_000:07d}")
    other = await _login(client, f"+7922{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    place_ids = await _places(client, 3)
    route_id = await _published_route(client, app, headers, place_ids[:2])
    try:
        before = (await client.get(f"/api/v1/routes/{route_id}/editable", headers=headers)).json()
        assert before["revision_status"] is None
        published_media_id = before["media"][0]["id"]
        # A review written about the first version.
        me = await client.get("/api/v1/me", headers=other_headers)
        assert me.status_code == 200, me.text
        reviewer_id = UUID(me.json()["id"])
        async with app.state.session_factory() as session:
            session.add(
                RouteReview(
                    id=uuid4(),
                    route_id=UUID(route_id),
                    author_user_id=reviewer_id,
                    body="Хороший маршрут",
                    rating=5,
                    status="published",
                    created_at=datetime.now(UTC) - timedelta(days=1),
                    updated_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
            await session.commit()

        saved = await client.post(
            "/api/v1/routes/drafts", headers=headers, json=_edit(route_id, place_ids)
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["id"] == route_id
        assert saved.json()["publication_status"] == "published"
        # «Сохранить» is a draft of the version, nothing is sent yet (D17).
        assert saved.json()["revision_status"] == "draft"

        # The catalogue keeps the published version.
        public = (await client.get(f"/api/v1/routes/{route_id}")).json()
        assert public["name"] == "Опубликованный маршрут"
        assert len(public["stops"]) == 2
        # The author sees one route, marked with its edit, and no extra row.
        mine = (await client.get("/api/v1/routes/mine", headers=headers)).json()["items"]
        assert [(item["id"], item["revision_status"]) for item in mine] == [(route_id, "draft")]
        # The editor opens on the edit, under the route's own id.
        editable = (await client.get(f"/api/v1/routes/{route_id}/editable", headers=headers)).json()
        assert editable["id"] == route_id
        assert editable["publication_status"] == "published"
        assert editable["revision_status"] == "draft"
        assert editable["name"] == "Обновлённый маршрут"
        assert [item["id"] for item in editable["places"]] == place_ids
        assert len(editable["media"]) == 1

        # Media is part of the version: an upload does not reach the
        # catalogue, and the id the file had on the published route still
        # names it for an editor opened before the edit began.
        upload = await client.post(
            f"/api/v1/routes/drafts/{route_id}/media",
            headers=headers,
            data={"position": "1"},
            files={"file": ("new.png", _png_bytes(), "image/png")},
        )
        assert upload.status_code == 200, upload.text
        synced = await client.put(
            f"/api/v1/routes/drafts/{route_id}/media",
            headers=headers,
            json={"keep": [published_media_id, upload.json()["id"]]},
        )
        assert synced.status_code == 204, synced.text
        editable = (await client.get(f"/api/v1/routes/{route_id}/editable", headers=headers)).json()
        assert len(editable["media"]) == 2
        assert len((await client.get(f"/api/v1/routes/{route_id}")).json()["media"]) == 1

        # The edit is not a route of its own: not by id, not for anyone.
        async with app.state.session_factory() as session:
            revision = await revision_of(session, UUID(route_id))
            assert revision is not None
            revision_id = str(revision.id)
        assert (await client.get(f"/api/v1/routes/{revision_id}")).status_code == 404
        for path in (
            f"/api/v1/routes/mine/{revision_id}",
            f"/api/v1/routes/{revision_id}/editable",
        ):
            assert (await client.get(path, headers=headers)).status_code == 404
        assert (
            await client.post(f"/api/v1/routes/{revision_id}/submit", headers=headers)
        ).status_code == 404

        # Sent only by the explicit submit; a second submit has nothing to send.
        submitted = await client.post(f"/api/v1/routes/{route_id}/submit", headers=headers)
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["publication_status"] == "published"
        assert submitted.json()["revision_status"] == "pending_review"
        assert (
            await client.post(f"/api/v1/routes/{route_id}/submit", headers=headers)
        ).status_code == 409
        # Editing again takes the waiting edit out of the queue.
        again = await client.post(
            "/api/v1/routes/drafts",
            headers=headers,
            json=_edit(route_id, place_ids, description="Вторая версия, уточнённая"),
        )
        assert again.json()["revision_status"] == "draft"
        assert (await client.post(f"/api/v1/routes/{route_id}/submit", headers=headers)).json()[
            "revision_status"
        ] == "pending_review"

        # What the moderator sees, and the approval.
        async with app.state.session_factory() as session:
            revision = await revision_of(session, UUID(route_id))
            assert revision is not None
            diff = await revision_diff(session, revision)
            assert diff is not None
            assert diff.name == ("Опубликованный маршрут", "Обновлённый маршрут")
            assert diff.description is not None
            assert len(diff.stops_added) == 1
            assert diff.stops_removed == ()
            assert (diff.media_added, diff.media_removed) == (1, 0)
            await apply_revision(session, revision, now=datetime.now(UTC))
            await session.commit()

        public = (await client.get(f"/api/v1/routes/{route_id}")).json()
        assert public["id"] == route_id
        assert public["publication_status"] == "published"
        assert public["name"] == "Обновлённый маршрут"
        assert [stop["place_id"] for stop in public["stops"]] == place_ids
        assert len(public["media"]) == 2
        assert public["geometry"] is not None
        mine = (await client.get("/api/v1/routes/mine", headers=headers)).json()["items"]
        assert [(item["id"], item["revision_status"]) for item in mine] == [(route_id, None)]
        # The old review stays, counted and marked (D6).
        reviews = (await client.get(f"/api/v1/routes/{route_id}/reviews")).json()
        assert [item["before_route_update"] for item in reviews["items"]] == [True]
        assert reviews["rating_count"] == 1
        # Foreign hands never reach any of it.
        assert (
            await client.delete(f"/api/v1/routes/{route_id}/revision", headers=other_headers)
        ).status_code == 404
    finally:
        async with app.state.session_factory() as session:
            await session.execute(delete(RouteReview).where(RouteReview.route_id == UUID(route_id)))
            await session.commit()
        await _cleanup(app, route_id)


@pytest.mark.asyncio
async def test_a_returned_edit_tells_why_and_can_be_dropped(
    publication_context: tuple[AsyncClient, Any],  # noqa: F811
) -> None:
    client, app = publication_context
    tokens = await _login(client, f"+7923{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    place_ids = await _places(client, 3)
    route_id = await _published_route(client, app, headers, place_ids[:2])
    try:
        await client.post("/api/v1/routes/drafts", headers=headers, json=_edit(route_id, place_ids))
        await client.post(f"/api/v1/routes/{route_id}/submit", headers=headers)
        async with app.state.session_factory() as session:
            revision = await revision_of(session, UUID(route_id))
            assert revision is not None
            revision.publication_status = "rejected"
            revision.rejection_reason = "publish_as_new"
            await session.commit()

        mine = (await client.get("/api/v1/routes/mine", headers=headers)).json()["items"]
        assert mine[0]["publication_status"] == "published"
        assert mine[0]["revision_status"] == "rejected"
        assert mine[0]["rejection_reason"]
        detail = (await client.get(f"/api/v1/routes/mine/{route_id}", headers=headers)).json()
        assert detail["revision_status"] == "rejected"
        assert detail["name"] == "Опубликованный маршрут"
        editable = (await client.get(f"/api/v1/routes/{route_id}/editable", headers=headers)).json()
        assert editable["rejection_reason"] == mine[0]["rejection_reason"]
        # Still in the catalogue, untouched.
        assert (await client.get(f"/api/v1/routes/{route_id}")).json()[
            "name"
        ] == "Опубликованный маршрут"

        dropped = await client.delete(f"/api/v1/routes/{route_id}/revision", headers=headers)
        assert dropped.status_code == 204
        assert (
            await client.delete(f"/api/v1/routes/{route_id}/revision", headers=headers)
        ).status_code == 204
        editable = (await client.get(f"/api/v1/routes/{route_id}/editable", headers=headers)).json()
        assert editable["revision_status"] is None
        assert editable["name"] == "Опубликованный маршрут"
        assert len(editable["media"]) == 1
    finally:
        await _cleanup(app, route_id)


@pytest.mark.asyncio
async def test_withdrawing_a_route_keeps_the_authors_latest_edit(
    publication_context: tuple[AsyncClient, Any],  # noqa: F811
) -> None:
    client, app = publication_context
    tokens = await _login(client, f"+7924{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    place_ids = await _places(client, 3)
    route_id = await _published_route(client, app, headers, place_ids[:2])
    try:
        await client.post("/api/v1/routes/drafts", headers=headers, json=_edit(route_id, place_ids))

        withdrawn = await client.post(f"/api/v1/routes/{route_id}/withdraw", headers=headers)
        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.json()["publication_status"] == "draft"
        assert withdrawn.json()["revision_status"] is None

        assert (await client.get(f"/api/v1/routes/{route_id}")).status_code == 404
        editable = (await client.get(f"/api/v1/routes/{route_id}/editable", headers=headers)).json()
        assert editable["publication_status"] == "draft"
        assert editable["name"] == "Обновлённый маршрут"
        assert len(editable["places"]) == 3
        async with app.state.session_factory() as session:
            assert await revision_of(session, UUID(route_id)) is None
    finally:
        await _cleanup(app, route_id)


def test_the_moderators_diff_names_what_changed() -> None:
    before = VersionFacts(
        name="Алушта",
        description="Старое",
        stops=("Набережная", "Ротонда", "Крепость", "Парк"),
        media=("a.jpg", "b.jpg"),
        transport_mode="walking",
        difficulty_level=2,
    )
    after = VersionFacts(
        name="Алушта",
        description="Новое",
        stops=("Ротонда", "Набережная", "Маяк"),
        media=("b.jpg", "c.jpg"),
        transport_mode="car",
        difficulty_level=2,
    )

    diff = diff_versions(before, after)

    assert diff.name is None
    assert diff.description == ("Старое", "Новое")
    assert diff.stops_added == ("Маяк",)
    assert diff.stops_removed == ("Крепость", "Парк")
    assert diff.stops_reordered is True
    assert (diff.media_added, diff.media_removed, diff.media_reordered) == (1, 1, False)
    assert diff.other == (("Способ передвижения", "walking", "car"),)
    # Half of the walk is gone: a hint to publish it as a new route.
    assert diff.stops_changed_share == 50
    assert diff.is_empty is False
    assert diff_versions(before, before).is_empty is True


class _Session:
    def add(self, _item: object) -> None:
        return None


@pytest.mark.asyncio
async def test_the_author_is_told_the_decision_is_about_the_edit() -> None:
    session: Any = _Session()
    approved = await notifications_service.create_route_moderation_notification(
        session,
        owner_user_id=uuid4(),
        route_id=uuid4(),
        route_name="Алушта",
        approved=True,
        revision=True,
    )
    assert approved.kind == "route_published"
    assert "заменил прежнюю версию" in approved.body

    returned = await notifications_service.create_route_moderation_notification(
        session,
        owner_user_id=uuid4(),
        route_id=uuid4(),
        route_name="Алушта",
        approved=False,
        reason="Опубликуйте как новый маршрут",
        revision=True,
    )
    assert returned.kind == "route_rejected"
    assert "Опубликуйте как новый маршрут" in returned.body
    assert "осталась прежняя версия" in returned.body

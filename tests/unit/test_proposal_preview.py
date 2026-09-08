from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.route_builder.application import generate_service


def _stored_proposal() -> SimpleNamespace:
    proposal_id, user_id = uuid4(), uuid4()
    return SimpleNamespace(
        id=proposal_id,
        user_id=user_id,
        status="draft",
        params={"city": "Ялта"},
        preview={
            "proposal_id": str(proposal_id),
            "title": "Ялта",
            "stops": [],
            "distance_meters": 4000,
            "trip_plan": {"days": [], "start_date": None},
        },
    )


async def test_preview_is_owned_and_reuses_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal = _stored_proposal()
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=proposal)
    route = AsyncMock()
    monkeypatch.setattr(generate_service, "_route_places", route)
    out = await generate_service.proposal_preview(
        session, user_id=proposal.user_id, proposal_id=proposal.id
    )
    assert out.distance_meters == 4000
    route.assert_not_awaited()
    session.add.assert_not_called()
    session.commit.assert_not_awaited()
    with pytest.raises(AppError) as error:
        await generate_service.proposal_preview(session, user_id=uuid4(), proposal_id=proposal.id)
    assert error.value.code == "proposal_not_found"


async def test_date_changes_do_not_accept_or_regenerate_proposal() -> None:
    proposal = _stored_proposal()
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=proposal)
    out = await generate_service.update_proposal_trip_date(
        session, user_id=proposal.user_id, proposal_id=proposal.id, start_date=date(2026, 9, 20)
    )
    assert proposal.status == "draft"
    assert proposal.params["trip_start_date"] == "2026-09-20"
    assert out.trip_plan is not None
    assert out.trip_plan.start_date == date(2026, 9, 20)
    assert proposal.preview["trip_plan"]["start_date"] == "2026-09-20"
    session.add.assert_not_called()

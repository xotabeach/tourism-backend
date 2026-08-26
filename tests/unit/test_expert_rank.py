"""Эксперт is a rank now, not a bare boolean disconnected from travel_ranks.

Before this, `is_expert` and `rank_id`/`rank_title` were entirely separate —
an expert's displayed rank still came from travel_points, and only the
leaderboard query had a bolted-on `is_expert = false` filter. These tests
lock in the new invariant: an expert's rank_id always tracks the dedicated
"Эксперт" row, is never recomputed from points, and reverts cleanly when
expert status is revoked.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from tourism_backend.modules.admin.presentation.views import _rank_id_for_expert_toggle
from tourism_backend.modules.identity.application.public_service import _resolve_rank
from tourism_backend.modules.identity.application.travel_points import _sync_rank
from tourism_backend.modules.identity.infrastructure.models import EXPERT_RANK_ID


class _FakeSession:
    def __init__(self, *, scalar_result: object = None, get_result: object = None) -> None:
        self.scalar_result = scalar_result
        self.get_result = get_result
        self.scalar_calls = 0
        self.get_calls = 0

    async def scalar(self, _stmt: object) -> object:
        self.scalar_calls += 1
        return self.scalar_result

    async def get(self, _model: object, _id: object) -> object:
        self.get_calls += 1
        return self.get_result


def _user(*, is_expert: bool, points: int, rank_id: object = None) -> SimpleNamespace:
    return SimpleNamespace(
        is_expert=is_expert,
        travel_points=points,
        rank_id=rank_id if rank_id is not None else uuid4(),
    )


@pytest.mark.asyncio
async def test_resolve_rank_assigns_expert_rank_without_touching_points() -> None:
    expert_rank = SimpleNamespace(id=EXPERT_RANK_ID, slug="expert", title="Эксперт")
    session = _FakeSession(get_result=expert_rank)
    user = _user(is_expert=True, points=50)

    rank = await _resolve_rank(session, user)  # type: ignore[arg-type]

    assert rank is expert_rank
    assert user.rank_id == EXPERT_RANK_ID
    assert session.scalar_calls == 0


@pytest.mark.asyncio
async def test_resolve_rank_falls_back_to_points_for_non_experts() -> None:
    novice = SimpleNamespace(id=uuid4(), slug="novice", title="Новичок")
    session = _FakeSession(scalar_result=novice)
    user = _user(is_expert=False, points=0)

    rank = await _resolve_rank(session, user)  # type: ignore[arg-type]

    assert rank is novice
    assert user.rank_id == novice.id
    assert session.get_calls == 0


@pytest.mark.asyncio
async def test_sync_rank_never_demotes_an_expert() -> None:
    """A user granted Эксперт earns +5 points from a like/favorite just like
    anyone else — that must not silently knock them back to a points rank."""
    session = _FakeSession(scalar_result=SimpleNamespace(id=uuid4()))
    user = _user(is_expert=True, points=100, rank_id=EXPERT_RANK_ID)

    await _sync_rank(session, user)  # type: ignore[arg-type]

    assert user.rank_id == EXPERT_RANK_ID
    assert session.scalar_calls == 0


@pytest.mark.asyncio
async def test_sync_rank_still_updates_regular_users() -> None:
    new_rank = SimpleNamespace(id=uuid4())
    session = _FakeSession(scalar_result=new_rank)
    user = _user(is_expert=False, points=1200)

    await _sync_rank(session, user)  # type: ignore[arg-type]

    assert user.rank_id == new_rank.id


@pytest.mark.asyncio
async def test_rank_id_for_expert_toggle_grant_and_revoke() -> None:
    user = _user(is_expert=False, points=6000, rank_id=uuid4())

    granted = await _rank_id_for_expert_toggle(_FakeSession(), user=user, is_expert=True)
    assert granted == EXPERT_RANK_ID

    fallback_rank = SimpleNamespace(id=uuid4())
    revoked = await _rank_id_for_expert_toggle(
        _FakeSession(scalar_result=fallback_rank), user=user, is_expert=False
    )
    assert revoked == fallback_rank.id


@pytest.mark.asyncio
async def test_rank_id_for_expert_toggle_revoke_keeps_current_rank_if_table_empty() -> None:
    """Defensive fallback: if no non-expert rank matches (shouldn't happen —
    novice has min_points=0 — but if the ranks table is ever empty), don't
    null out rank_id."""
    current = uuid4()
    user = _user(is_expert=True, points=0, rank_id=current)

    revoked = await _rank_id_for_expert_toggle(
        _FakeSession(scalar_result=None), user=user, is_expert=False
    )

    assert revoked == current

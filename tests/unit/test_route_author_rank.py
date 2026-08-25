"""Route cards must carry the owner's real travel rank, not a fixed label.

The mobile card used to print «Продвинутый пешеход» under every non-editorial
author because the route payload carried no rank at all.
"""

from collections.abc import Sequence
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tourism_backend.modules.routes.application.service import _rank_titles


class _FakeScalars:
    def __init__(self, rows: Sequence[object]) -> None:
        self._rows = rows

    def all(self) -> Sequence[object]:
        return self._rows


class _FakeSession:
    """Returns the rank table for the single ``select(TravelRank)`` call."""

    def __init__(self, ranks: Sequence[object]) -> None:
        self._ranks = ranks
        self.calls = 0

    async def scalars(self, _stmt: object) -> _FakeScalars:
        self.calls += 1
        return _FakeScalars(self._ranks)


def _rank(title: str, min_points: int) -> SimpleNamespace:
    return SimpleNamespace(title=title, min_points=min_points)


def _user(points: int) -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), travel_points=points)


RANKS = [
    _rank("Новичок", 0),
    _rank("Путешественник", 100),
    _rank("Продвинутый пешеход", 500),
    _rank("Легенда Крыма", 2000),
]


@pytest.mark.asyncio
async def test_rank_title_matches_travel_points() -> None:
    novice = _user(0)
    traveller = _user(150)
    advanced = _user(500)
    legend = _user(9001)
    session = _FakeSession(RANKS)

    titles = await _rank_titles(
        session,  # type: ignore[arg-type]
        [novice, traveller, advanced, legend],  # type: ignore[list-item]
    )

    assert titles[novice.id] == "Новичок"
    assert titles[traveller.id] == "Путешественник"
    # Boundary: exactly at min_points already earns the rank.
    assert titles[advanced.id] == "Продвинутый пешеход"
    assert titles[legend.id] == "Легенда Крыма"


@pytest.mark.asyncio
async def test_rank_titles_reads_the_rank_table_once_for_many_users() -> None:
    session = _FakeSession(RANKS)

    await _rank_titles(
        session,  # type: ignore[arg-type]
        [_user(10), _user(20), _user(30)],  # type: ignore[list-item]
    )

    assert session.calls == 1


@pytest.mark.asyncio
async def test_no_users_skips_the_query() -> None:
    session = _FakeSession(RANKS)

    assert await _rank_titles(session, []) == {}  # type: ignore[arg-type]
    assert session.calls == 0

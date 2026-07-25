from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tourism_backend.modules.geography.application.service import (
    _coords_for_localities,
    _coords_for_regions,
)
from tourism_backend.modules.places.application.service import _coords_for_places


class _Rows:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[object, ...]]:
        return self._rows


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "loader",
    [_coords_for_regions, _coords_for_localities, _coords_for_places],
)
async def test_coordinate_loader_batches_ids_into_one_query(loader) -> None:
    first_id = uuid4()
    second_id = uuid4()
    session = AsyncMock()
    session.execute.return_value = _Rows(
        [
            (first_id, 34.1, 44.2),
            (second_id, 35.3, 45.4),
        ]
    )

    coordinates = await loader(session, [first_id, second_id])

    session.execute.assert_awaited_once()
    assert coordinates[first_id] == (34.1, 44.2)
    assert coordinates[second_id] == (35.3, 45.4)


@pytest.mark.asyncio
async def test_coordinate_loader_does_not_query_for_empty_ids() -> None:
    session = AsyncMock()

    coordinates = await _coords_for_places(session, [])

    session.execute.assert_not_awaited()
    assert coordinates == {}

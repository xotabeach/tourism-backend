import os
from collections.abc import AsyncIterator
from datetime import date, time

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tourism_backend.modules.transit.infrastructure.importer import import_transit, parse_transit
from tourism_backend.modules.transit.infrastructure.models import (
    TransitLine,
    TransitSchedule,
    TransitStop,
    TransitVariant,
    TransitVariantStop,
)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism",
)
_PREFIX = "test-transit-"


def _payload(*route_ids: int) -> dict[str, object]:
    return {
        "variants": [
            {
                "osm_id": route_id,
                "kind": "bus",
                "ptv2": True,
                "ref": "110",
                "name": f"Автобус 110 ({route_id})",
                "stops": [
                    {"osm_id": f"{_PREFIX}a", "role": "stop", "lng": 34.1, "lat": 44.9},
                    {"osm_id": f"{_PREFIX}b", "role": "stop", "lng": 34.3, "lat": 44.7},
                ],
                "shape": [[34.1, 44.9], [34.3, 44.7]],
            }
            for route_id in route_ids
        ],
        "masters": [],
    }


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1 FROM transit_lines LIMIT 1"))
    except Exception:  # noqa: BLE001
        await engine.dispose()
        pytest.skip("Postgres with transit tables is unavailable")
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        # Rolled back at the end: the import never commits by itself.
        yield db
        await db.rollback()
    await engine.dispose()


async def test_reimport_keeps_editor_fields_and_marks_missing(session: AsyncSession) -> None:
    # Ids far from real OSM relations, so the local import is not touched.
    kept, dropped = 990_000_001, 990_000_002
    await import_transit(session, parse_transit(_payload(kept, dropped)), data_version="t1")

    line = (
        await session.execute(select(TransitLine).where(TransitLine.osm_id == f"v{kept}"))
    ).scalar_one()
    line.status = "suspended"
    line.suspend_reason = "Ремонт дороги"
    session.add(
        TransitSchedule(
            line_id=line.id,
            title="Лето",
            days=127,
            first_departure=time(6, 0),
            last_departure=time(22, 0),
            headway_minutes=20,
            source="звонок перевозчику",
            checked_at=date(2026, 9, 24),
        )
    )
    await session.flush()

    report = await import_transit(session, parse_transit(_payload(kept)), data_version="t2")

    session.expire_all()
    line = (
        await session.execute(select(TransitLine).where(TransitLine.osm_id == f"v{kept}"))
    ).scalar_one()
    assert (line.status, line.suspend_reason, line.data_version) == (
        "suspended",
        "Ремонт дороги",
        "t2",
    )
    schedules = (
        await session.execute(select(TransitSchedule).where(TransitSchedule.line_id == line.id))
    ).scalars()
    assert [schedule.title for schedule in schedules] == ["Лето"]

    gone = (
        await session.execute(select(TransitLine).where(TransitLine.osm_id == f"v{dropped}"))
    ).scalar_one()
    assert gone.status == "missing"
    variant = (
        await session.execute(select(TransitVariant).where(TransitVariant.osm_id == f"r{dropped}"))
    ).scalar_one()
    assert not variant.present_in_osm
    assert report.missing_lines >= 1

    stops = (
        await session.execute(
            select(TransitVariantStop.seq, TransitStop.osm_id)
            .join(TransitStop, TransitStop.id == TransitVariantStop.stop_id)
            .join(TransitVariant, TransitVariant.id == TransitVariantStop.variant_id)
            .where(TransitVariant.osm_id == f"r{kept}")
            .order_by(TransitVariantStop.seq)
        )
    ).all()
    assert [osm_id for _, osm_id in stops] == [f"{_PREFIX}a", f"{_PREFIX}b"]

    # Back in OSM: the lost line is active again.
    await import_transit(session, parse_transit(_payload(kept, dropped)), data_version="t3")
    session.expire_all()
    gone = (
        await session.execute(select(TransitLine).where(TransitLine.osm_id == f"v{dropped}"))
    ).scalar_one()
    assert gone.status == "active"

"""Lines, variants and stops from the OSM build (spec 12b, section 1).

The build writes ``transit.json`` next to the Valhalla graph. An import
refreshes what OSM knows and keeps what editors entered: a line's status and
suspension reason, its timetables. A line or variant gone from OSM is kept
and marked, never deleted, so its timetable is not lost to a broken
relation (D7, D8).

  docker compose exec -T backend python -m \\
      tourism_backend.modules.transit.infrastructure.importer osm20260922 \\
      < osm/current/transit.json
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from geoalchemy2 import WKTElement
from sqlalchemy import case, delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.transit.infrastructure.models import (
    LINE_KINDS,
    TransitLine,
    TransitStop,
    TransitVariant,
    TransitVariantStop,
)

_BATCH = 500


@dataclass(frozen=True, slots=True)
class ParsedStop:
    osm_id: str
    name: str | None
    lng: float
    lat: float


@dataclass(frozen=True, slots=True)
class ParsedVariant:
    osm_id: str
    name: str | None
    from_name: str | None
    to_name: str | None
    ptv2: bool
    shape: list[tuple[float, float]]
    osm_interval: str | None
    osm_opening_hours: str | None
    stops: list[tuple[str, str | None]]  # (stop osm_id, role) in order

    @property
    def needs_mapping(self) -> bool:
        # Without PTv2 the member order is not the travel order (D8).
        return not self.ptv2 or len(self.stops) < 2


@dataclass(slots=True)
class ParsedLine:
    osm_id: str
    kind: str
    ref: str | None
    name: str
    operator: str | None
    network: str | None
    colour: str | None
    variants: list[ParsedVariant] = field(default_factory=list)

    @property
    def needs_mapping(self) -> bool:
        return all(variant.needs_mapping for variant in self.variants)


@dataclass(frozen=True, slots=True)
class ParsedTransit:
    lines: list[ParsedLine]
    stops: list[ParsedStop]


@dataclass(frozen=True, slots=True)
class ImportReport:
    lines: int
    variants: int
    stops: int
    needs_mapping: int
    missing_lines: int


def _text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def _variant(raw: Mapping[str, Any]) -> ParsedVariant:
    return ParsedVariant(
        osm_id=f"r{raw['osm_id']}",
        name=_text(raw.get("name"), 255),
        from_name=_text(raw.get("from"), 255),
        to_name=_text(raw.get("to"), 255),
        ptv2=bool(raw.get("ptv2")),
        shape=[(float(lng), float(lat)) for lng, lat in raw.get("shape") or []],
        osm_interval=_text(raw.get("interval"), 64),
        osm_opening_hours=_text(raw.get("opening_hours"), 255),
        stops=[
            (str(stop["osm_id"]), _text(stop.get("role"), 32)) for stop in raw.get("stops") or []
        ],
    )


def _line_name(ref: str | None, names: Sequence[str | None], kind: str) -> str:
    for name in names:
        if name:
            return name[:255]
    return f"{kind} {ref}" if ref else kind


def parse_transit(payload: Mapping[str, Any]) -> ParsedTransit:
    """Group variants into lines: by their route_master when OSM has one,
    else each lone route is a line of its own."""
    raw_variants = {
        int(raw["osm_id"]): raw
        for raw in payload.get("variants") or []
        if raw.get("kind") in LINE_KINDS
    }
    stops: dict[str, ParsedStop] = {}
    for raw in raw_variants.values():
        for stop in raw.get("stops") or []:
            stops.setdefault(
                str(stop["osm_id"]),
                ParsedStop(
                    osm_id=str(stop["osm_id"]),
                    name=_text(stop.get("name"), 255),
                    lng=float(stop["lng"]),
                    lat=float(stop["lat"]),
                ),
            )

    lines: list[ParsedLine] = []
    grouped: set[int] = set()
    for master in payload.get("masters") or []:
        members = [raw_variants[ref] for ref in master.get("routes") or [] if ref in raw_variants]
        members = [raw for raw in members if int(raw["osm_id"]) not in grouped]
        if not members:
            continue
        grouped.update(int(raw["osm_id"]) for raw in members)
        first = members[0]
        names = [_text(master.get("name"), 255), _text(first.get("name"), 255)]
        ref = _text(master.get("ref") or first.get("ref"), 64)
        kind = first["kind"]
        lines.append(
            ParsedLine(
                osm_id=f"m{master['osm_id']}",
                kind=kind,
                ref=ref,
                name=_line_name(ref, names, kind),
                operator=_text(master.get("operator") or first.get("operator"), 255),
                network=_text(master.get("network") or first.get("network"), 255),
                colour=_text(first.get("colour"), 16),
                variants=[_variant(raw) for raw in members],
            )
        )
    for osm_id, raw in raw_variants.items():
        if osm_id in grouped:
            continue
        ref = _text(raw.get("ref"), 64)
        lines.append(
            ParsedLine(
                osm_id=f"v{osm_id}",
                kind=raw["kind"],
                ref=ref,
                name=_line_name(ref, [_text(raw.get("name"), 255)], raw["kind"]),
                operator=_text(raw.get("operator"), 255),
                network=_text(raw.get("network"), 255),
                colour=_text(raw.get("colour"), 16),
                variants=[_variant(raw)],
            )
        )
    return ParsedTransit(lines=lines, stops=list(stops.values()))


def _point(lng: float, lat: float) -> WKTElement:
    return WKTElement(f"POINT({lng} {lat})", srid=4326)


def _linestring(shape: Sequence[tuple[float, float]]) -> WKTElement | None:
    if len(shape) < 2:
        return None
    return WKTElement(
        "LINESTRING(" + ", ".join(f"{lng} {lat}" for lng, lat in shape) + ")", srid=4326
    )


async def import_transit(
    session: AsyncSession, parsed: ParsedTransit, *, data_version: str
) -> ImportReport:
    """Upsert everything by OSM id; the caller commits."""
    for start in range(0, len(parsed.stops), _BATCH):
        rows = [
            {"osm_id": stop.osm_id, "name": stop.name, "location": _point(stop.lng, stop.lat)}
            for stop in parsed.stops[start : start + _BATCH]
        ]
        statement = pg_insert(TransitStop).values(rows)
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[TransitStop.osm_id],
                set_={
                    "name": statement.excluded.name,
                    "location": statement.excluded.location,
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )
    stop_ids = dict(
        (await session.execute(select(TransitStop.osm_id, TransitStop.id))).tuples().all()
    )

    line_ids: dict[str, Any] = {}
    for line in parsed.lines:
        line_insert = pg_insert(TransitLine).values(
            osm_id=line.osm_id,
            kind=line.kind,
            ref=line.ref,
            name=line.name,
            operator=line.operator,
            network=line.network,
            colour=line.colour,
            needs_mapping=line.needs_mapping,
            data_version=data_version,
        )
        # Status and suspension are the editors'; only a line OSM had lost
        # comes back to active.
        excluded = line_insert.excluded
        line_upsert = line_insert.on_conflict_do_update(
            index_elements=[TransitLine.osm_id],
            set_={
                "kind": excluded.kind,
                "ref": excluded.ref,
                "name": excluded.name,
                "operator": excluded.operator,
                "network": excluded.network,
                "colour": excluded.colour,
                "needs_mapping": excluded.needs_mapping,
                "data_version": excluded.data_version,
                "status": case(
                    (TransitLine.status == "missing", "active"), else_=TransitLine.status
                ),
                "updated_at": excluded.updated_at,
            },
        ).returning(TransitLine.id)
        line_ids[line.osm_id] = (await session.execute(line_upsert)).scalar_one()

    variant_count = 0
    for line in parsed.lines:
        for variant in line.variants:
            values: dict[str, Any] = {
                "line_id": line_ids[line.osm_id],
                "osm_id": variant.osm_id,
                "name": variant.name,
                "from_name": variant.from_name,
                "to_name": variant.to_name,
                "ptv2": variant.ptv2,
                "needs_mapping": variant.needs_mapping,
                "shape": _linestring(variant.shape),
                "osm_interval": variant.osm_interval,
                "osm_opening_hours": variant.osm_opening_hours,
                "present_in_osm": True,
            }
            variant_insert = pg_insert(TransitVariant).values(**values)
            variant_upsert = variant_insert.on_conflict_do_update(
                index_elements=[TransitVariant.osm_id],
                set_={
                    **{key: variant_insert.excluded[key] for key in values if key != "osm_id"},
                    "updated_at": variant_insert.excluded.updated_at,
                },
            ).returning(TransitVariant.id)
            variant_id = (await session.execute(variant_upsert)).scalar_one()
            await session.execute(
                delete(TransitVariantStop).where(TransitVariantStop.variant_id == variant_id)
            )
            stop_rows: list[dict[str, Any]] = [
                {"variant_id": variant_id, "seq": seq, "stop_id": stop_ids[osm_id], "role": role}
                for seq, (osm_id, role) in enumerate(variant.stops)
            ]
            if stop_rows:
                await session.execute(insert(TransitVariantStop), stop_rows)
            variant_count += 1

    seen_variants = [variant.osm_id for line in parsed.lines for variant in line.variants]
    await session.execute(
        update(TransitVariant)
        .where(TransitVariant.osm_id.not_in(seen_variants))
        .values(present_in_osm=False)
    )
    missing = await session.execute(
        update(TransitLine)
        .where(
            TransitLine.osm_id.not_in(list(line_ids)),
            TransitLine.osm_id.not_like("manual-%"),
            TransitLine.status != "missing",
        )
        .values(status="missing")
        .returning(TransitLine.id)
    )
    return ImportReport(
        lines=len(parsed.lines),
        variants=variant_count,
        stops=len(parsed.stops),
        needs_mapping=sum(1 for line in parsed.lines if line.needs_mapping),
        missing_lines=len(missing.all()),
    )


async def _main(data_version: str) -> None:
    from tourism_backend.config import get_settings
    from tourism_backend.db.session import create_engine, create_session_factory

    parsed = parse_transit(json.load(sys.stdin))
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session:
            report = await import_transit(session, parsed, data_version=data_version)
            await session.commit()
    finally:
        await engine.dispose()
    sys.stdout.write(
        f"transit loaded ({data_version}): {report.lines} lines, {report.variants} variants, "
        f"{report.stops} stops, {report.needs_mapping} lines need mapping, "
        f"{report.missing_lines} newly missing\n"
    )


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1]))

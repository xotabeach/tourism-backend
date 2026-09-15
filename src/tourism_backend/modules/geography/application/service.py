import re
from uuid import UUID

from geoalchemy2 import Geometry
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import Select, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.geography.application.schemas import (
    CountryOut,
    LocalityOut,
    LocationSuggestionOut,
    RegionOut,
)
from tourism_backend.modules.geography.infrastructure.models import Country, Locality, Region
from tourism_backend.modules.places.infrastructure.models import Place

_WORDS_RE = re.compile(r"[0-9a-zа-я]+", re.IGNORECASE)
_LOCALITY_TYPE_LABELS = {
    "city": "город",
    "town": "посёлок",
    "village": "село",
    "hamlet": "деревня",
}


def _fold(value: str) -> str:
    return " ".join(_WORDS_RE.findall(value.casefold().replace("ё", "е")))


def _word_stem(value: str) -> str:
    """Small Russian-name fallback, not a general morphology engine."""

    if len(value) <= 4:
        return value
    if value[-1] in "аяоеыийьую":
        return value[:-1]
    return value


def _same_name_word(actual: str, expected: str) -> bool:
    actual_stem = _word_stem(actual)
    expected_stem = _word_stem(expected)
    if min(len(actual_stem), len(expected_stem)) < 3:
        return actual_stem == expected_stem
    if min(len(actual_stem), len(expected_stem)) == 3:
        return actual_stem == expected_stem
    return actual_stem.startswith(expected_stem) or expected_stem.startswith(actual_stem)


def _name_score(query: str, names: list[str]) -> int:
    query_folded = _fold(query)
    if not query_folded:
        return 0
    query_words = query_folded.split()
    best = 0
    for raw in names:
        name = _fold(raw)
        if not name:
            continue
        if name == query_folded:
            best = max(best, 100)
        elif name.startswith(query_folded):
            best = max(best, 90)
        elif query_folded.startswith(name):
            best = max(best, 86)
        elif query_folded in name:
            best = max(best, 78)
        name_words = name.split()
        if len(query_words) == len(name_words) and all(
            _same_name_word(left, right)
            for left, right in zip(query_words, name_words, strict=True)
        ):
            best = max(best, 74)
    return best


def locality_names_mentioned(text: str, localities: list[Locality]) -> list[Locality]:
    """Find arbitrary catalogue localities in a user utterance.

    Names come from data, not a city allowlist.  The conservative stem rule
    covers common forms such as «Фороса», «Симеиза» and «в Алупке» while
    avoiding fuzzy matches against unrelated short words.
    """

    folded_text = _fold(text)
    words = folded_text.split()
    mentioned: list[Locality] = []
    for locality in localities:
        variants = [locality.name, *(locality.aliases or [])]
        found = False
        for variant in variants:
            name = _fold(variant)
            if not name:
                continue
            if f" {name} " in f" {folded_text} ":
                found = True
                break
            name_words = name.split()
            if len(name_words) == 1 and len(name_words[0]) >= 4:
                stem = _word_stem(name_words[0])
                if any(_same_name_word(word, stem) for word in words):
                    found = True
                    break
            elif len(name_words) > 1:
                for index in range(0, len(words) - len(name_words) + 1):
                    window = words[index : index + len(name_words)]
                    if all(
                        _same_name_word(actual, expected)
                        for actual, expected in zip(window, name_words, strict=True)
                    ):
                        found = True
                        break
        if found:
            mentioned.append(locality)
    return mentioned[:8]


async def list_countries(session: AsyncSession) -> list[CountryOut]:
    result = await session.scalars(
        select(Country).where(Country.status == "active").order_by(Country.name)
    )
    return [CountryOut.model_validate(row) for row in result.all()]


async def _coords_for_regions(
    session: AsyncSession,
    region_ids: list[UUID],
) -> dict[UUID, tuple[float | None, float | None]]:
    if not region_ids:
        return {}
    geom = cast(Region.center, Geometry)
    rows = (
        await session.execute(
            select(Region.id, ST_X(geom), ST_Y(geom)).where(Region.id.in_(region_ids))
        )
    ).all()
    return {
        region_id: (
            float(lng) if lng is not None else None,
            float(lat) if lat is not None else None,
        )
        for region_id, lng, lat in rows
    }


async def _coords_for_localities(
    session: AsyncSession,
    locality_ids: list[UUID],
) -> dict[UUID, tuple[float | None, float | None]]:
    if not locality_ids:
        return {}
    geom = cast(Locality.center, Geometry)
    rows = (
        await session.execute(
            select(Locality.id, ST_X(geom), ST_Y(geom)).where(Locality.id.in_(locality_ids))
        )
    ).all()
    return {
        locality_id: (
            float(lng) if lng is not None else None,
            float(lat) if lat is not None else None,
        )
        for locality_id, lng, lat in rows
    }


async def list_regions(session: AsyncSession, *, country_code: str | None) -> list[RegionOut]:
    stmt: Select[tuple[Region]] = select(Region).where(Region.status == "active")
    if country_code:
        stmt = stmt.join(Country, Country.id == Region.country_id).where(
            Country.code == country_code.upper()
        )
    stmt = stmt.order_by(Region.name)
    regions = (await session.scalars(stmt)).all()
    coords = await _coords_for_regions(session, [region.id for region in regions])

    out: list[RegionOut] = []
    for region in regions:
        lng, lat = coords.get(region.id, (None, None))
        payload = RegionOut.model_validate(region)
        out.append(payload.model_copy(update={"center_lng": lng, "center_lat": lat}))
    return out


async def list_localities(session: AsyncSession, *, region_slug: str) -> list[LocalityOut]:
    stmt = (
        select(Locality)
        .join(Region, Region.id == Locality.region_id)
        .where(Locality.status == "active", Region.slug == region_slug)
        .order_by(Locality.name)
    )
    localities = (await session.scalars(stmt)).all()
    coords = await _coords_for_localities(session, [locality.id for locality in localities])
    out: list[LocalityOut] = []
    for locality in localities:
        lng, lat = coords.get(locality.id, (None, None))
        payload = LocalityOut.model_validate(locality)
        out.append(payload.model_copy(update={"center_lng": lng, "center_lat": lat}))
    return out


async def mentioned_localities(
    session: AsyncSession,
    *,
    text: str,
    region_slug: str = "crimea",
) -> list[Locality]:
    rows = list(
        (
            await session.scalars(
                select(Locality)
                .join(Region, Region.id == Locality.region_id)
                .where(Locality.status == "active", Region.slug == region_slug)
                .order_by(Locality.name)
                .limit(2_000)
            )
        ).all()
    )
    return locality_names_mentioned(text, rows)


async def search_locations(
    session: AsyncSession,
    *,
    query: str,
    region_slug: str = "crimea",
    limit: int = 12,
) -> list[LocationSuggestionOut]:
    """Search locality kinds and published POIs for route endpoints.

    This endpoint intentionally reads only the project's own catalogue. A
    provider suggestion can be layered on later, but must not be persisted as
    if it were first-party data.
    """

    cleaned = " ".join(query.split())[:80]
    if len(_fold(cleaned)) < 2:
        return []
    bounded_limit = max(1, min(limit, 20))
    region = await session.scalar(
        select(Region).where(Region.slug == region_slug, Region.status == "active")
    )
    if region is None:
        return []

    locality_rows = list(
        (
            await session.scalars(
                select(Locality)
                .where(Locality.region_id == region.id, Locality.status == "active")
                .order_by(Locality.name)
                .limit(2_000)
            )
        ).all()
    )
    locality_coords = await _coords_for_localities(
        session, [locality.id for locality in locality_rows]
    )
    ranked_localities: list[tuple[int, str, LocationSuggestionOut]] = []
    for locality in locality_rows:
        score = _name_score(cleaned, [locality.name, *(locality.aliases or [])])
        lng, lat = locality_coords.get(locality.id, (None, None))
        if score <= 0 or lng is None or lat is None:
            continue
        type_label = _LOCALITY_TYPE_LABELS.get(locality.type, "населённый пункт")
        ranked_localities.append(
            (
                score + min((locality.population or 0) // 50_000, 4),
                locality.name,
                LocationSuggestionOut(
                    kind="locality",
                    id=locality.id,
                    name=locality.name,
                    subtitle=type_label,
                    locality_type=locality.type,
                    center_lng=lng,
                    center_lat=lat,
                ),
            )
        )

    query_words = _fold(cleaned).split()
    longest_query_word = max(query_words, key=len)
    prefix = longest_query_word[: max(3, min(6, len(longest_query_word)))]
    pattern = f"%{prefix}%"
    place_geom = cast(Place.location, Geometry)
    place_rows = (
        await session.execute(
            select(
                Place,
                Locality.name,
                ST_X(place_geom),
                ST_Y(place_geom),
            )
            .outerjoin(Locality, Locality.id == Place.locality_id)
            .where(
                Place.region_id == region.id,
                Place.publication_status == "published",
                Place.merged_into_place_id.is_(None),
                Place.data_quality_status != "rejected",
                or_(
                    Place.name.ilike(pattern),
                    Place.address.ilike(pattern),
                    Place.short_description.ilike(pattern),
                ),
            )
            .order_by(Place.name)
            .limit(80)
        )
    ).all()
    ranked_places: list[tuple[int, str, LocationSuggestionOut]] = []
    for place, locality_name, lng, lat in place_rows:
        score = _name_score(cleaned, [place.name])
        if score <= 0 or lng is None or lat is None:
            continue
        ranked_places.append(
            (
                score - 2,
                place.name,
                LocationSuggestionOut(
                    kind="place",
                    id=place.id,
                    name=place.name,
                    subtitle=str(locality_name) if locality_name else "точка маршрута",
                    center_lng=float(lng),
                    center_lat=float(lat),
                ),
            )
        )

    ranked = sorted(
        [*ranked_localities, *ranked_places],
        key=lambda item: (-item[0], item[1]),
    )
    return [item for _, _, item in ranked[:bounded_limit]]

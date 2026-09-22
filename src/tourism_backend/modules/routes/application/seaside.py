"""The route's «Море» tag (BACKEND-19).

A route is a sea route when at least one stop is a beach or stands close to
the shore. The shore is the OSM coastline already imported for the terrain
gate (`route_terrain_features`, `scripts/import_terrain_features.py`).
"""

from collections.abc import Sequence
from uuid import UUID

from geoalchemy2.functions import ST_DWithin
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.places.infrastructure.models import Category, Place, PlaceCategory
from tourism_backend.modules.route_builder.infrastructure.models import RouteTerrainFeature

# How close to the coastline a stop counts as «у моря». The coastline is
# accurate, but a catalogue point is often set a few hundred metres inland
# (Ласточкино гнездо 220–290 m, Судакская крепость 330 m, галерея
# Айвазовского 380 m), so the margin covers that; Ливадийский дворец
# (480 m) and inland sights stay out.
SEASIDE_DISTANCE_METERS = 400


async def is_seaside(session: AsyncSession, place_ids: Sequence[UUID]) -> bool:
    if not place_ids:
        return False
    on_beach = exists().where(
        PlaceCategory.place_id == Place.id,
        Category.id == PlaceCategory.category_id,
        Category.slug == "beach",
    )
    near_shore = exists().where(
        RouteTerrainFeature.kind == "coastline",
        ST_DWithin(RouteTerrainFeature.geometry, Place.location, SEASIDE_DISTANCE_METERS),
    )
    stmt = select(exists().where(Place.id.in_(list(place_ids)), on_beach | near_shore))
    return bool(await session.scalar(stmt))

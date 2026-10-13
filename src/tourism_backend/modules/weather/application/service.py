"""Today's weather along a route, with warnings and an honest failure.

Spec 15: D9 (a block on the route page and before the start; the backend
asks, the cache is by district, the source sits behind one interface, Open-
Meteo on the beta; the matching does not use it) and D20 (on a failure a
cache up to 6 hours old with its time, otherwise an explicit «нет данных»,
never an empty block that reads as «безопасно»).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from geoalchemy2 import Geometry
from geoalchemy2.functions import ST_X, ST_Y
from redis.asyncio import Redis
from sqlalchemy import cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.routes.infrastructure.models import RouteStop
from tourism_backend.modules.weather.application.models import DayForecast, WeatherProvider

_logger = logging.getLogger("tourism_backend.weather")

#: A district: a cell of 0.1° (about 11 km north-south, 8 km east-west in
#: Crimea). Points of a route in one cell share one request.
_CELL_DEGREES = 0.1
#: Fresh enough to serve without asking again.
FRESH_FOR = timedelta(hours=1)
#: How old a cached forecast may be when the source is down (D20).
STALE_LIMIT = timedelta(hours=6)
#: A route asks for at most this many districts: start, middle, finish.
_MAX_CELLS = 3

HEAT_C = 32.0
WIND_MS = 12.0
GUST_MS = 15.0
_THUNDER_CODES = range(95, 100)

NO_DATA_MESSAGE = "Нет данных о погоде, проверьте прогноз сами"

WeatherStatus = Literal["ok", "stale", "unavailable"]


@dataclass(frozen=True, slots=True)
class WeatherWarning:
    kind: Literal["thunderstorm", "heat", "wind"]
    text: str


@dataclass(frozen=True, slots=True)
class RouteWeather:
    status: WeatherStatus
    forecast: DayForecast | None = None
    #: When the oldest forecast used was fetched.
    fetched_at: datetime | None = None
    warnings: tuple[WeatherWarning, ...] = field(default_factory=tuple)


def today_in_crimea() -> date:
    return datetime.now(ZoneInfo("Europe/Simferopol")).date()


def cell_of(lat: float, lng: float) -> tuple[float, float]:
    return (
        round(round(lat / _CELL_DEGREES) * _CELL_DEGREES, 1),
        round(round(lng / _CELL_DEGREES) * _CELL_DEGREES, 1),
    )


def _cache_key(cell: tuple[float, float], day: date) -> str:
    return f"weather:v1:{cell[0]:.1f}:{cell[1]:.1f}:{day.isoformat()}"


def warnings_for(forecast: DayForecast) -> tuple[WeatherWarning, ...]:
    found: list[WeatherWarning] = []
    if forecast.weather_code in _THUNDER_CODES:
        found.append(WeatherWarning("thunderstorm", "Гроза: в горах и на открытых местах опасно"))
    if forecast.temp_max_c >= HEAT_C:
        found.append(
            WeatherWarning("heat", f"Жара до {round(forecast.temp_max_c)} °C: возьмите воду")
        )
    gust = forecast.gust_max_ms
    if forecast.wind_max_ms >= WIND_MS or (gust is not None and gust >= GUST_MS):
        strongest = max(forecast.wind_max_ms, gust or 0)
        found.append(WeatherWarning("wind", f"Сильный ветер, порывы до {round(strongest)} м/с"))
    return tuple(found)


def combine(forecasts: list[DayForecast]) -> DayForecast:
    """The route's day as its worst point: what to prepare for."""
    gusts = [f.gust_max_ms for f in forecasts if f.gust_max_ms is not None]
    precip = [
        f.precipitation_probability for f in forecasts if f.precipitation_probability is not None
    ]
    return DayForecast(
        temp_min_c=min(f.temp_min_c for f in forecasts),
        temp_max_c=max(f.temp_max_c for f in forecasts),
        precipitation_probability=max(precip) if precip else None,
        wind_max_ms=max(f.wind_max_ms for f in forecasts),
        gust_max_ms=max(gusts) if gusts else None,
        weather_code=max(f.weather_code for f in forecasts),
    )


async def _cell_forecast(
    cell: tuple[float, float],
    *,
    day: date,
    provider: WeatherProvider,
    redis: Redis,
    now: datetime,
) -> tuple[DayForecast, datetime] | None:
    key = _cache_key(cell, day)
    cached: tuple[DayForecast, datetime] | None = None
    try:
        raw = await redis.get(key)
        if raw:
            data = json.loads(raw)
            cached = (
                DayForecast.from_json(data["forecast"]),
                datetime.fromisoformat(data["fetched_at"]),
            )
    except Exception:  # noqa: BLE001 - a broken cache is a miss
        _logger.warning("weather_cache_unreadable key=%s", key, exc_info=True)
    if cached is not None and now - cached[1] < FRESH_FOR:
        return cached
    try:
        forecast = await provider.today(cell[0], cell[1])
    except Exception:  # noqa: BLE001 - the source failing is expected, D20
        _logger.warning("weather_source_failed provider=%s cell=%s", provider.name, cell)
        if cached is not None and now - cached[1] <= STALE_LIMIT:
            return cached
        return None
    try:
        await redis.set(
            key,
            json.dumps({"forecast": forecast.to_json(), "fetched_at": now.isoformat()}),
            ex=int((STALE_LIMIT + timedelta(hours=1)).total_seconds()),
        )
    except Exception:  # noqa: BLE001
        _logger.warning("weather_cache_write_failed key=%s", key, exc_info=True)
    return forecast, now


async def route_points(session: AsyncSession, route_id: UUID) -> list[tuple[float, float]]:
    geom = cast(Place.location, Geometry)
    rows = (
        await session.execute(
            select(ST_Y(geom), ST_X(geom))
            .join(RouteStop, RouteStop.place_id == Place.id)
            .where(RouteStop.route_id == route_id)
            .order_by(RouteStop.position)
        )
    ).all()
    return [(float(lat), float(lng)) for lat, lng in rows if lat is not None and lng is not None]


def route_cells(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Start, middle and finish districts, without repeats."""
    if not points:
        return []
    picks = [points[0], points[len(points) // 2], points[-1]]
    cells: list[tuple[float, float]] = []
    for lat, lng in picks:
        cell = cell_of(lat, lng)
        if cell not in cells:
            cells.append(cell)
    return cells[:_MAX_CELLS]


async def weather_for_points(
    points: list[tuple[float, float]],
    *,
    provider: WeatherProvider | None,
    redis: Redis,
    now: datetime | None = None,
    day: date | None = None,
) -> RouteWeather:
    now = now or datetime.now(UTC)
    day = day or today_in_crimea()
    cells = route_cells(points)
    if provider is None or not cells:
        return RouteWeather(status="unavailable")
    found: list[tuple[DayForecast, datetime]] = []
    for cell in cells:
        result = await _cell_forecast(cell, day=day, provider=provider, redis=redis, now=now)
        if result is not None:
            found.append(result)
    if not found:
        return RouteWeather(status="unavailable")
    oldest = min(fetched for _, fetched in found)
    # A district missing, or an old forecast in use: say how old it is.
    stale = len(found) < len(cells) or now - oldest >= FRESH_FOR
    forecast = combine([item for item, _ in found])
    return RouteWeather(
        status="stale" if stale else "ok",
        forecast=forecast,
        fetched_at=oldest,
        warnings=warnings_for(forecast),
    )

"""Weather along a route (BACKEND-40, spec 15 D9/D20)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from tourism_backend.modules.weather.application.models import DayForecast
from tourism_backend.modules.weather.application.service import (
    cell_of,
    combine,
    route_cells,
    warnings_for,
    weather_for_points,
)

DAY = date(2026, 7, 10)
NOW = datetime(2026, 7, 10, 9, 0, tzinfo=UTC)


def _forecast(**changes: Any) -> DayForecast:
    base: dict[str, Any] = {
        "temp_min_c": 18.0,
        "temp_max_c": 27.0,
        "precipitation_probability": 10,
        "wind_max_ms": 4.0,
        "gust_max_ms": 7.0,
        "weather_code": 1,
    }
    return DayForecast(**{**base, **changes})


class _Redis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value


class _Provider:
    name = "fake"
    attribution = "Погода: тест"

    def __init__(self, forecast: DayForecast | None = None) -> None:
        self.forecast = forecast or _forecast()
        self.calls = 0
        self.down = False

    async def today(self, lat: float, lng: float) -> DayForecast:
        self.calls += 1
        if self.down:
            raise TimeoutError("source is down")
        return self.forecast


YALTA = (44.495, 34.166)
AI_PETRI = (44.451, 34.054)


def test_warnings_for_storm_heat_and_wind() -> None:
    assert warnings_for(_forecast()) == ()
    kinds = [w.kind for w in warnings_for(_forecast(weather_code=95, temp_max_c=33.4))]
    assert kinds == ["thunderstorm", "heat"]
    wind = warnings_for(_forecast(gust_max_ms=16.2))
    assert [w.kind for w in wind] == ["wind"]
    assert "16 м/с" in wind[0].text


def test_the_route_day_is_its_worst_point() -> None:
    day = combine([_forecast(), _forecast(temp_max_c=31, wind_max_ms=9, weather_code=61)])
    assert day.temp_max_c == 31
    assert day.wind_max_ms == 9
    assert day.weather_code == 61


def test_points_in_one_district_share_a_request() -> None:
    assert cell_of(*YALTA) == (44.5, 34.2)
    assert route_cells([YALTA, (44.49, 34.17), (44.50, 34.16)]) == [(44.5, 34.2)]
    assert len(route_cells([YALTA, (44.6, 34.4), AI_PETRI])) == 3
    assert route_cells([]) == []


@pytest.mark.asyncio
async def test_fresh_forecast_is_cached_for_an_hour() -> None:
    redis, provider = _Redis(), _Provider()
    first = await weather_for_points([YALTA], provider=provider, redis=redis, now=NOW, day=DAY)
    again = await weather_for_points(
        [YALTA], provider=provider, redis=redis, now=NOW + timedelta(minutes=30), day=DAY
    )
    assert first.status == again.status == "ok"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_a_failure_serves_a_cache_up_to_six_hours_with_its_time() -> None:
    redis, provider = _Redis(), _Provider()
    await weather_for_points([YALTA], provider=provider, redis=redis, now=NOW, day=DAY)
    provider.down = True
    later = NOW + timedelta(hours=5)
    stale = await weather_for_points([YALTA], provider=provider, redis=redis, now=later, day=DAY)
    assert stale.status == "stale"
    assert stale.fetched_at == NOW
    too_late = await weather_for_points(
        [YALTA], provider=provider, redis=redis, now=NOW + timedelta(hours=7), day=DAY
    )
    assert too_late.status == "unavailable"
    assert too_late.forecast is None


@pytest.mark.asyncio
async def test_no_source_or_no_points_says_there_is_no_data() -> None:
    redis = _Redis()
    assert (await weather_for_points([YALTA], provider=None, redis=redis)).status == "unavailable"
    nothing = await weather_for_points([], provider=_Provider(), redis=redis)
    assert nothing.status == "unavailable"

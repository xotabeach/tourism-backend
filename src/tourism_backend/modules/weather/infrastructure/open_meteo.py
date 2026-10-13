"""Open-Meteo daily forecast (https://open-meteo.com), CC BY 4.0."""

from __future__ import annotations

import httpx

from tourism_backend.modules.weather.application.models import DayForecast

_FREE = "https://api.open-meteo.com/v1/forecast"
_PAID = "https://customer-api.open-meteo.com/v1/forecast"
_DAILY = (
    "temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
    "wind_speed_10m_max,wind_gusts_10m_max,weather_code"
)


class OpenMeteoProvider:
    name = "open_meteo"
    attribution = "Погода: Open-Meteo.com (CC BY 4.0)"

    def __init__(self, api_key: str | None = None, *, timeout: float = 6.0) -> None:
        self._api_key = api_key
        self._timeout = timeout

    async def today(self, lat: float, lng: float) -> DayForecast:
        params: dict[str, str | float | int] = {
            "latitude": lat,
            "longitude": lng,
            "daily": _DAILY,
            "wind_speed_unit": "ms",
            "timezone": "Europe/Simferopol",
            "forecast_days": 1,
        }
        url = _FREE
        if self._api_key:
            url = _PAID
            params["apikey"] = self._api_key
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(url, params=params)
        response.raise_for_status()
        daily = response.json()["daily"]

        def first(key: str) -> float | None:
            values = daily.get(key) or [None]
            value = values[0]
            return float(value) if value is not None else None

        temp_max = first("temperature_2m_max")
        temp_min = first("temperature_2m_min")
        wind = first("wind_speed_10m_max")
        code = first("weather_code")
        if temp_max is None or temp_min is None or wind is None or code is None:
            raise ValueError("Open-Meteo returned no forecast for today")
        precip = first("precipitation_probability_max")
        gust = first("wind_gusts_10m_max")
        return DayForecast(
            temp_min_c=float(temp_min),
            temp_max_c=float(temp_max),
            precipitation_probability=int(precip) if precip is not None else None,
            wind_max_ms=float(wind),
            gust_max_ms=float(gust) if gust is not None else None,
            weather_code=int(code),
        )

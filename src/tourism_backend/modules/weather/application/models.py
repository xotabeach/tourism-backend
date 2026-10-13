"""Weather for a route's points today (BACKEND-40, spec 15 D9/D20)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class DayForecast:
    """Today at one place, as the provider gives it."""

    temp_min_c: float
    temp_max_c: float
    precipitation_probability: int | None
    wind_max_ms: float
    gust_max_ms: float | None
    weather_code: int

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DayForecast:
        return cls(
            temp_min_c=float(data["temp_min_c"]),
            temp_max_c=float(data["temp_max_c"]),
            precipitation_probability=(
                int(data["precipitation_probability"])
                if data.get("precipitation_probability") is not None
                else None
            ),
            wind_max_ms=float(data["wind_max_ms"]),
            gust_max_ms=float(data["gust_max_ms"]) if data.get("gust_max_ms") is not None else None,
            weather_code=int(data["weather_code"]),
        )


class WeatherProvider(Protocol):
    """A forecast source behind one interface; Open-Meteo on the beta."""

    name: str
    attribution: str

    async def today(self, lat: float, lng: float) -> DayForecast: ...

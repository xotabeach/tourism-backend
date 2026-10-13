"""Today's weather along a route (BACKEND-40)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from tourism_backend.api.deps import DbSession, OptionalCurrentUserId, RedisClient, SettingsDep
from tourism_backend.api.errors import AppError
from tourism_backend.config import Settings
from tourism_backend.modules.routes.infrastructure.models import Route
from tourism_backend.modules.weather.application.models import WeatherProvider
from tourism_backend.modules.weather.application.service import (
    NO_DATA_MESSAGE,
    route_points,
    weather_for_points,
)
from tourism_backend.modules.weather.infrastructure.open_meteo import OpenMeteoProvider

router = APIRouter(tags=["weather"])


class WeatherWarningOut(BaseModel):
    kind: Literal["thunderstorm", "heat", "wind"]
    text: str


class RouteWeatherOut(BaseModel):
    #: ok: fresh; stale: an older forecast (see fetched_at) or a district
    #: missing; unavailable: nothing to show, say so (spec 15, D20).
    status: Literal["ok", "stale", "unavailable"]
    fetched_at: datetime | None = None
    temp_min_c: float | None = None
    temp_max_c: float | None = None
    precipitation_probability: int | None = None
    wind_max_ms: float | None = None
    gust_max_ms: float | None = None
    weather_code: int | None = None
    warnings: list[WeatherWarningOut] = Field(default_factory=list)
    #: The line to show when there is no forecast.
    message: str | None = None
    attribution: str | None = None


def weather_provider(request: Request, settings: Settings) -> WeatherProvider | None:
    """The source in use; tests put their own on ``app.state``."""
    override = getattr(request.app.state, "weather_provider", None)
    if override is not None:
        return override  # type: ignore[no-any-return]
    if not settings.weather_enabled:
        return None
    key = settings.open_meteo_api_key
    return OpenMeteoProvider(key.get_secret_value() if key else None)


@router.get("/routes/{route_id}/weather", response_model=RouteWeatherOut)
async def get_route_weather(
    route_id: UUID,
    request: Request,
    response: Response,
    session: DbSession,
    redis: RedisClient,
    settings: SettingsDep,
    user_id: OptionalCurrentUserId,
) -> RouteWeatherOut:
    route = await session.get(Route, route_id)
    visible = route is not None and (
        route.visibility == "public" or (user_id is not None and route.owner_user_id == user_id)
    )
    if route is None or not visible:
        raise AppError(code="not_found", message="Route not found", status_code=404)
    provider = weather_provider(request, settings)
    result = await weather_for_points(
        await route_points(session, route.id), provider=provider, redis=redis
    )
    response.headers["Cache-Control"] = "private, max-age=600"
    forecast = result.forecast
    return RouteWeatherOut(
        status=result.status,
        fetched_at=result.fetched_at,
        temp_min_c=forecast.temp_min_c if forecast else None,
        temp_max_c=forecast.temp_max_c if forecast else None,
        precipitation_probability=forecast.precipitation_probability if forecast else None,
        wind_max_ms=forecast.wind_max_ms if forecast else None,
        gust_max_ms=forecast.gust_max_ms if forecast else None,
        weather_code=forecast.weather_code if forecast else None,
        warnings=[WeatherWarningOut(kind=w.kind, text=w.text) for w in result.warnings],
        message=NO_DATA_MESSAGE if result.status == "unavailable" else None,
        attribution=provider.attribution if provider is not None and forecast else None,
    )

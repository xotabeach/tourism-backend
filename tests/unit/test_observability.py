import asyncio
import logging
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tourism_backend.api.observability import SlowRequestLogMiddleware, watch_event_loop_lag


def _app(threshold: float) -> FastAPI:
    app = FastAPI()

    @app.get("/users/{user_id}")
    async def user(user_id: str) -> dict[str, str]:
        await asyncio.sleep(0.05)
        return {"id": user_id}

    app.add_middleware(SlowRequestLogMiddleware, threshold=threshold)
    return app


@pytest.mark.asyncio
async def test_slow_request_is_logged_by_route_template(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="tourism_backend.observability")
    async with AsyncClient(transport=ASGITransport(app=_app(0.01)), base_url="http://t") as client:
        response = await client.get("/users/secret-id")

    assert response.status_code == 200
    records = [r for r in caplog.records if r.getMessage() == "slow_request"]
    assert len(records) == 1
    assert records[0].route == "/users/{user_id}"  # type: ignore[attr-defined]
    assert records[0].status == 200  # type: ignore[attr-defined]
    assert "secret-id" not in str(records[0].__dict__)


@pytest.mark.asyncio
async def test_fast_request_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="tourism_backend.observability")
    async with AsyncClient(transport=ASGITransport(app=_app(5)), base_url="http://t") as client:
        await client.get("/users/1")

    assert not [r for r in caplog.records if r.getMessage() == "slow_request"]


@pytest.mark.asyncio
async def test_a_blocked_event_loop_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="tourism_backend.observability")
    watch = asyncio.create_task(watch_event_loop_lag(interval=0.02, lag_threshold=0.05))
    await asyncio.sleep(0.03)
    time.sleep(0.15)  # noqa: ASYNC251 - the blocking work this watch must report
    await asyncio.sleep(0.05)
    watch.cancel()

    assert [r for r in caplog.records if r.getMessage() == "event_loop_lag"]

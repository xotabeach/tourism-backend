"""Slow requests and a stalled event loop, written to the log (BACKEND-6).

The API runs as one uvicorn process: a handler that does blocking work on the
event loop holds up every other request until it is done. Both measurements
here exist to see that from the logs before users report it:

- ``slow_request``: a request that took longer than ``threshold`` seconds,
  with its route template (no ids), method, status and duration;
- ``event_loop_lag``: the loop woke up later than it asked to, by more than
  ``lag_threshold`` seconds, i.e. something kept it busy for that long.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("tourism_backend.observability")

SLOW_REQUEST_SECONDS = 1.0
LOOP_LAG_SECONDS = 0.25
LOOP_PROBE_INTERVAL_SECONDS = 1.0


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str):
        return path
    # Unmatched paths (404s, static files) are logged by their mount only, so
    # user ids and file names never end up in the log.
    raw = str(scope.get("path", ""))
    return "/" + raw.strip("/").split("/", 1)[0] if raw else ""


class SlowRequestLogMiddleware:
    """Logs HTTP requests slower than ``threshold`` seconds."""

    def __init__(self, app: ASGIApp, threshold: float = SLOW_REQUEST_SECONDS) -> None:
        self.app = app
        self.threshold = threshold

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 0

        async def send_with_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            elapsed = time.perf_counter() - started
            if elapsed >= self.threshold:
                logger.warning(
                    "slow_request",
                    extra={
                        "method": scope.get("method"),
                        "route": _route_template(scope),
                        "status": status or None,
                        "duration_ms": round(elapsed * 1000),
                    },
                )


async def watch_event_loop_lag(
    *,
    interval: float = LOOP_PROBE_INTERVAL_SECONDS,
    lag_threshold: float = LOOP_LAG_SECONDS,
) -> None:
    """Runs until cancelled; logs whenever the loop was blocked for a while."""
    loop = asyncio.get_running_loop()
    while True:
        expected = loop.time() + interval
        await asyncio.sleep(interval)
        lag = loop.time() - expected
        if lag >= lag_threshold:
            logger.warning("event_loop_lag", extra={"lag_ms": round(lag * 1000)})

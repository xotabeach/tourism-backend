"""Lightweight Origin/Referer check for cookie-authenticated /admin mutations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.datastructures import URL
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp

_SAFE = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _origin_ok(request: Request) -> bool:
    host = request.headers.get("host")
    if not host:
        return False
    origin = request.headers.get("origin")
    if origin:
        try:
            return URL(origin).netloc == host
        except Exception:  # noqa: BLE001
            return False
    referer = request.headers.get("referer")
    if referer:
        try:
            return URL(referer).netloc == host
        except Exception:  # noqa: BLE001
            return False
    # No Origin/Referer — reject mutating admin requests (CSRF-ish).
    return False


class AdminCsrfMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, path_prefix: str = "/admin") -> None:
        super().__init__(app)
        self._prefix = path_prefix.rstrip("/") or "/admin"

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        on_admin = path == self._prefix or path.startswith(self._prefix + "/")
        if on_admin and request.method not in _SAFE and not _origin_ok(request):
            return PlainTextResponse("CSRF validation failed", status_code=403)
        return await call_next(request)

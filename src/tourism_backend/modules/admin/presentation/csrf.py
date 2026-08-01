"""Lightweight Origin/Referer check for cookie-authenticated /admin mutations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.datastructures import URL
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp

_SAFE = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _request_host(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-host")
    if forwarded:
        # First hop only; strip optional port for comparison below.
        return forwarded.split(",")[0].strip()
    return request.headers.get("host")


def _hosts_match(candidate_netloc: str, host: str) -> bool:
    # Compare hostnames case-insensitively; ignore default ports.
    cand = candidate_netloc.lower()
    expected = host.lower()
    if cand == expected:
        return True
    return cand.split(":")[0] == expected.split(":")[0]


def _origin_ok(request: Request) -> bool:
    host = _request_host(request)
    if not host:
        return False
    origin = request.headers.get("origin")
    if origin:
        try:
            return _hosts_match(URL(origin).netloc, host)
        except Exception:  # noqa: BLE001
            return False
    referer = request.headers.get("referer")
    if referer:
        try:
            return _hosts_match(URL(referer).netloc, host)
        except Exception:  # noqa: BLE001
            return False
    # Caddy may send Referrer-Policy: no-referrer; browsers still send Origin on
    # POST. If both are absent, reject.
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

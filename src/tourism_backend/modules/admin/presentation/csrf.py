"""CSRF checks for cookie-authenticated /admin mutations.

Strategy (OWASP-aligned, proxy-friendly):
1. Reject explicit cross-site signals (`Sec-Fetch-Site: cross-site`, bad Origin).
2. Accept matching `Origin` / `Referer` host against Host / X-Forwarded-Host.
3. Accept modern same-origin attestation via `Sec-Fetch-Site` when browsers omit
   Origin/Referer (common with upstream `Referrer-Policy: no-referrer`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.datastructures import URL
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp

_SAFE = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_SAME_SITE_FETCH = frozenset({"same-origin", "same-site", "none"})


def _request_host(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-host")
    if forwarded:
        # First hop only; strip optional port for comparison below.
        return forwarded.split(",")[0].strip()
    host = request.headers.get("host")
    if host:
        return host
    # Fallback after ProxyHeadersMiddleware / URL rebuild.
    return request.url.hostname


def _hosts_match(candidate_netloc: str, host: str) -> bool:
    # Compare hostnames case-insensitively; ignore default ports.
    cand = candidate_netloc.lower()
    expected = host.lower()
    if cand == expected:
        return True
    return cand.split(":")[0] == expected.split(":")[0]


def _netloc_ok(raw_url: str, host: str) -> bool:
    try:
        netloc = URL(raw_url).netloc
    except Exception:  # noqa: BLE001
        return False
    if not netloc:
        return False
    return _hosts_match(netloc, host)


def _origin_ok(request: Request) -> bool:
    fetch_site = (request.headers.get("sec-fetch-site") or "").lower()
    if fetch_site == "cross-site":
        return False

    host = _request_host(request)
    if not host:
        return False

    origin = request.headers.get("origin")
    if origin:
        # Opaque / redirected origins must not pass.
        if origin.strip().lower() == "null":
            return False
        return _netloc_ok(origin, host)

    referer = request.headers.get("referer")
    if referer:
        return _netloc_ok(referer, host)

    # Caddy may still send Referrer-Policy: no-referrer (deploy of platform
    # assets is separate from backend image). Modern browsers still send
    # Sec-Fetch-Site on form POSTs even when Origin/Referer are stripped.
    return fetch_site in _SAME_SITE_FETCH


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

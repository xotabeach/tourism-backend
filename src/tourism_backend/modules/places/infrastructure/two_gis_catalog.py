"""2GIS Places catalog HTTP client.

Application code never sees the API key or the raw query string. The client
fails closed on non-HTTPS, oversized bodies, and 429.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

_ITEMS_PATH = "/3.0/items"
_MAX_RESPONSE_BYTES = 1_000_000
_PAGE_SIZE = 5
_FIELDS = (
    "items.point,items.address_name,items.full_address_name,items.schedule,items.name,items.rubrics"
)


class TwoGisCatalogError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TwoGisCatalogClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://catalog.api.2gis.com",
        timeout_seconds: float = 10,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("2GIS API key must not be empty")
        if not base_url.startswith("https://"):
            raise ValueError("2GIS catalog URL must use HTTPS")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._client = client

    def search(
        self,
        *,
        query: str,
        lng: float,
        lat: float,
        radius_m: int,
    ) -> dict[str, Any]:
        q = " ".join(query.split())[:200]
        if not q:
            raise TwoGisCatalogError("catalog_request_invalid", "Search query is empty")
        if not 1 <= radius_m <= 50_000:
            raise TwoGisCatalogError("catalog_request_invalid", "Search radius is out of range")
        params = {
            "q": q,
            "point": f"{lng:.6f},{lat:.6f}",
            "radius": str(radius_m),
            "page_size": str(_PAGE_SIZE),
            "locale": "ru_RU",
            "fields": _FIELDS,
            "key": self._api_key,
        }
        try:
            response = self._get(params)
            if response.status_code == 429:
                raise TwoGisCatalogError("catalog_quota_exceeded", "2GIS catalog quota exceeded")
            if response.status_code >= 400:
                raise TwoGisCatalogError(
                    f"catalog_http_{response.status_code}",
                    "2GIS catalog is unavailable",
                )
            if len(response.content) > _MAX_RESPONSE_BYTES:
                raise TwoGisCatalogError(
                    "catalog_provider_error",
                    "2GIS catalog response is too large",
                )
            data = response.json()
        except TwoGisCatalogError:
            raise
        except httpx.TimeoutException as exc:
            raise TwoGisCatalogError("catalog_timeout", "2GIS catalog timed out") from exc
        except httpx.HTTPError as exc:
            raise TwoGisCatalogError(
                "catalog_provider_error",
                "2GIS catalog is unavailable",
            ) from exc
        except ValueError as exc:
            raise TwoGisCatalogError(
                "catalog_provider_error",
                "2GIS returned invalid JSON",
            ) from exc
        if not isinstance(data, dict):
            raise TwoGisCatalogError(
                "catalog_provider_error",
                "2GIS returned an invalid response",
            )
        meta = data.get("meta")
        if isinstance(meta, dict):
            code = meta.get("code")
            if code == 404:
                # Catalog uses 404 for "no items", not a transport failure.
                return {"meta": {"code": 404}, "result": {"items": []}}
            if isinstance(code, int) and code >= 400:
                raise TwoGisCatalogError(
                    f"catalog_meta_{code}",
                    "2GIS catalog rejected the request",
                )
        return data

    def _get(self, params: Mapping[str, str]) -> httpx.Response:
        if self._client is not None:
            return self._client.get(f"{self._base_url}{_ITEMS_PATH}", params=dict(params))
        with httpx.Client(
            timeout=self._timeout,
            headers={"User-Agent": "CrimeaTrip-2gis-enrich/1.0"},
        ) as client:
            return client.get(f"{self._base_url}{_ITEMS_PATH}", params=dict(params))

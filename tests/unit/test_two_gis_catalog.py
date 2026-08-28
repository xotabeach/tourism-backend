"""Contract tests for the 2GIS Places catalog client."""

from __future__ import annotations

import httpx
import pytest

from tourism_backend.modules.places.infrastructure.two_gis_catalog import (
    TwoGisCatalogClient,
    TwoGisCatalogError,
)


def test_catalog_search_omits_key_from_typed_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/3.0/items"
        assert request.url.params["key"] == "test-secret"
        assert request.url.params["locale"] == "ru_RU"
        assert "test-secret" not in str(request.url.path)
        body = {
            "result": {
                "items": [
                    {
                        "id": "1",
                        "name": "Музей",
                        "point": {"lat": 44.5, "lon": 34.1},
                    }
                ]
            }
        }
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        catalog = TwoGisCatalogClient(api_key="test-secret", client=client)
        payload = catalog.search(query="Музей", lng=34.1, lat=44.5, radius_m=250)

    items = payload["result"]["items"]
    assert items[0]["name"] == "Музей"


def test_catalog_http_429_is_quota_error_without_key() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "quota test-secret"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        catalog = TwoGisCatalogClient(api_key="test-secret", client=client)
        with pytest.raises(TwoGisCatalogError) as error:
            catalog.search(query="Ялта", lng=34.17, lat=44.50, radius_m=250)

    assert error.value.code == "catalog_quota_exceeded"
    assert "test-secret" not in error.value.message


def test_catalog_rejects_empty_query_before_network() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        catalog = TwoGisCatalogClient(api_key="test-secret", client=client)
        with pytest.raises(TwoGisCatalogError) as error:
            catalog.search(query="   ", lng=34.1, lat=44.5, radius_m=250)

    assert error.value.code == "catalog_request_invalid"
    assert calls == 0


def test_catalog_rejects_non_https_base_url() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        TwoGisCatalogClient(api_key="test-secret", base_url="http://catalog.api.2gis.com")


def test_catalog_oversized_body_is_provider_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{" + (b"x" * 1_000_001) + b"}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        catalog = TwoGisCatalogClient(api_key="test-secret", client=client)
        with pytest.raises(TwoGisCatalogError) as error:
            catalog.search(query="Ялта", lng=34.17, lat=44.50, radius_m=250)

    assert error.value.code == "catalog_provider_error"
    assert "test-secret" not in error.value.message


def test_catalog_http_500_does_not_echo_key() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "invalid key test-secret"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        catalog = TwoGisCatalogClient(api_key="test-secret", client=client)
        with pytest.raises(TwoGisCatalogError) as error:
            catalog.search(query="Ялта", lng=34.17, lat=44.50, radius_m=250)

    assert error.value.code == "catalog_http_500"
    assert "test-secret" not in error.value.message
    assert "test-secret" not in str(error.value)


def test_catalog_http_200_with_meta_400_is_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["locale"] == "ru_RU"
        return httpx.Response(
            200,
            json={"meta": {"code": 400, "error": {"message": "bad locale test-secret"}}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        catalog = TwoGisCatalogClient(api_key="test-secret", client=client)
        with pytest.raises(TwoGisCatalogError) as error:
            catalog.search(query="Ялта", lng=34.17, lat=44.50, radius_m=250)

    assert error.value.code == "catalog_meta_400"
    assert "test-secret" not in error.value.message
    assert "bad locale" not in error.value.message


def test_catalog_meta_404_is_empty_result_not_an_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "meta": {
                    "code": 404,
                    "error": {"type": "itemNotFound", "message": "test-secret"},
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        catalog = TwoGisCatalogClient(api_key="test-secret", client=client)
        payload = catalog.search(query="Ялта", lng=34.17, lat=44.50, radius_m=250)

    assert payload["result"]["items"] == []
    assert "test-secret" not in str(payload.get("result"))

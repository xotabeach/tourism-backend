import pytest
from httpx import ASGITransport, AsyncClient

from tourism_backend.config import Settings
from tourism_backend.main import create_app
from tourism_backend.modules.route_builder.infrastructure.two_gis_routing import (
    reset_two_gis_routing_state_for_tests,
)


@pytest.fixture
def app():
    # Explicit override: local dev .env may set a real 2GIS key, which would
    # otherwise leak through as "configured": true in this isolated test.
    return create_app(Settings(routing_provider="stub", two_gis_http_api_key=None))


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    reset_two_gis_routing_state_for_tests()


@pytest.mark.asyncio
async def test_two_gis_status_reports_unconfigured_without_key(app) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/maps/two-gis/status")

    assert response.status_code == 200
    body = response.json()
    assert body["routing_provider"] == "stub"
    assert body["configured"] is False
    assert body["circuit_state"] == "closed"
    assert body["calls_total"] == 0
    assert "test-secret" not in response.text

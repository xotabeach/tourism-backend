import pytest
from httpx import ASGITransport, AsyncClient

from tourism_backend.main import create_app


@pytest.fixture
def app():
    return create_app()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health/live", "/health"])
async def test_health_live_returns_ok(app, path: str) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health/ready", "/ready"])
async def test_ready_reports_not_ready_without_lifespan(app, path: str) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"

import pytest
from httpx import ASGITransport, AsyncClient

from tourism_backend.api.deps import get_db_session
from tourism_backend.api.errors import AppError
from tourism_backend.main import create_app


@pytest.fixture
def app():
    return create_app()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/v1", "/api/v1/"])
async def test_api_v1_root(app, path: str) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)

    assert response.status_code == 200
    assert response.json() == {"name": "tourism-backend", "api_version": "v1"}


@pytest.mark.asyncio
async def test_app_error_handler_returns_stable_body(app) -> None:
    @app.get("/_test/app-error")
    async def raise_app_error() -> None:
        raise AppError(code="demo_error", message="demo failed", status_code=409)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/_test/app-error")

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "demo_error", "message": "demo failed"},
    }


@pytest.mark.asyncio
async def test_http_exception_uses_error_envelope(app) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/missing-route")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "http_error"
    assert "message" in body["error"]


@pytest.mark.asyncio
async def test_validation_error_does_not_reflect_submitted_input(app) -> None:
    async def fake_db_session():
        yield object()

    app.dependency_overrides[get_db_session] = fake_db_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/routes",
            params={"limit": "do-not-reflect-this-secret"},
        )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert "do-not-reflect-this-secret" not in str(body)
    assert "input" not in body["error"]["details"][0]

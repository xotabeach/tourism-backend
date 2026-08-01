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
async def test_ready_reports_not_ready_without_dependencies(app, path: str) -> None:
    # create_app wires engine clients early for SQLAdmin; readiness still requires
    # live session_factory + redis on app.state.
    app.state.session_factory = None
    app.state.redis = None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


class _Session:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _statement) -> None:
        if self._error is not None:
            raise self._error


class _SessionFactory:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def __call__(self) -> _Session:
        return _Session(self._error)


class _Redis:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    async def ping(self) -> bool:
        if self._error is not None:
            raise self._error
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_error", "redis_error", "dependency"),
    [
        (RuntimeError("db-secret-host"), None, "database"),
        (None, RuntimeError("redis-secret-host"), "redis"),
    ],
)
async def test_ready_does_not_expose_dependency_exception(
    app,
    session_error: Exception | None,
    redis_error: Exception | None,
    dependency: str,
) -> None:
    app.state.session_factory = _SessionFactory(session_error)
    app.state.redis = _Redis(redis_error)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "detail": f"{dependency} unavailable",
    }
    assert "secret-host" not in response.text

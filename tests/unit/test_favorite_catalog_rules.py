"""M-3: favorited routes must match public catalog publication rules."""

from types import SimpleNamespace

from tourism_backend.modules.favorites.application.service import is_catalog_favorite_route


def _route(**overrides: str) -> SimpleNamespace:
    base = {
        "source": "editorial",
        "visibility": "public",
        "lifecycle_status": "active",
        "publication_status": "published",
    }
    return SimpleNamespace(**{**base, **overrides})


def test_catalog_favorite_route_accepts_published_editorial() -> None:
    assert is_catalog_favorite_route(_route()) is True  # type: ignore[arg-type]


def test_catalog_favorite_route_rejects_missing_publication_status() -> None:
    assert (
        is_catalog_favorite_route(_route(publication_status="draft")) is False  # type: ignore[arg-type]
    )
    assert (
        is_catalog_favorite_route(_route(lifecycle_status="draft")) is False  # type: ignore[arg-type]
    )
    assert is_catalog_favorite_route(_route(visibility="private")) is False  # type: ignore[arg-type]
    assert is_catalog_favorite_route(_route(source="generated")) is False  # type: ignore[arg-type]

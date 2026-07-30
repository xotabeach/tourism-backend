"""Unit coverage for media attachment path helpers."""

from tourism_backend.modules.media.application.service import (
    public_path_from_storage_key,
    storage_key_from_public_path,
)


def test_media_path_helpers_roundtrip() -> None:
    assert public_path_from_storage_key("profiles/u/a.webp") == "/media/profiles/u/a.webp"
    assert public_path_from_storage_key("/media/profiles/u/a.webp") == "/media/profiles/u/a.webp"
    assert storage_key_from_public_path("/media/profiles/u/a.webp") == "profiles/u/a.webp"
    assert storage_key_from_public_path("profiles/u/a.webp") == "profiles/u/a.webp"

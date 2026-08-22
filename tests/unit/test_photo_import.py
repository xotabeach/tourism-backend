"""Unit tests for the Wikimedia Commons place-photo lookup module."""

from __future__ import annotations

import httpx
import pytest

from tourism_backend.modules.places.application.photo_import import (
    WikimediaCommonsClient,
    commons_title_from_tags,
    is_license_allowed,
    strip_html,
)


def test_commons_title_from_wikimedia_commons_tag() -> None:
    assert (
        commons_title_from_tags({"wikimedia_commons": "File:Swallows_Nest.jpg"})
        == "File:Swallows_Nest.jpg"
    )


def test_commons_title_from_image_tag_file_prefix() -> None:
    assert commons_title_from_tags({"image": "File:Foo.jpg"}) == "File:Foo.jpg"


def test_commons_title_from_allowlisted_image_url() -> None:
    tags = {"image": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Foo.jpg"}
    assert commons_title_from_tags(tags) == "File:Foo.jpg"


def test_commons_title_rejects_arbitrary_image_url() -> None:
    # Explicit anti-goal: never follow an off-Wikimedia URL as a photo source.
    tags = {"image": "https://random-site.example/photo.jpg"}
    assert commons_title_from_tags(tags) is None


def test_commons_title_none_when_no_tags() -> None:
    assert commons_title_from_tags({}) is None
    assert commons_title_from_tags({"wikimedia_commons": "Category:Something"}) is None


@pytest.mark.parametrize(
    "license_name",
    ["CC0", "Public domain", "PDM 1.0", "CC BY-SA 4.0", "CC BY 3.0", "cc by-sa 2.0"],
)
def test_license_allowlist_accepts_free_licenses(license_name: str) -> None:
    assert is_license_allowed(license_name) is True


@pytest.mark.parametrize(
    "license_name",
    [None, "", "All rights reserved", "GFDL", "CC BY-NC 4.0", "CC BY-ND 4.0"],
)
def test_license_allowlist_rejects_everything_else(license_name: str | None) -> None:
    assert is_license_allowed(license_name) is False


def test_strip_html_removes_tags_and_collapses_whitespace() -> None:
    raw = '<a href="//commons.wikimedia.org/wiki/User:Foo" title="User:Foo">Jane  Doe</a>'
    assert strip_html(raw) == "Jane Doe"


def _client(handler: httpx.MockTransport) -> WikimediaCommonsClient:
    return WikimediaCommonsClient(transport=handler)


def test_fetch_file_info_parses_allowlisted_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "commons.wikimedia.org"
        assert request.url.params["titles"] == "File:Swallows_Nest.jpg"
        return httpx.Response(
            200,
            json={
                "query": {
                    "pages": [
                        {
                            "title": "File:Swallows_Nest.jpg",
                            "imageinfo": [
                                {
                                    "url": "https://upload.wikimedia.org/x/Swallows_Nest.jpg",
                                    "descriptionurl": "https://commons.wikimedia.org/wiki/File:Swallows_Nest.jpg",
                                    "mime": "image/jpeg",
                                    "width": 4000,
                                    "height": 3000,
                                    "extmetadata": {
                                        "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                        "Artist": {"value": "<span>Jane Doe</span>"},
                                    },
                                }
                            ],
                        }
                    ]
                }
            },
        )

    info = _client(httpx.MockTransport(handler)).fetch_file_info("File:Swallows_Nest.jpg")
    assert info is not None
    assert info.image_url == "https://upload.wikimedia.org/x/Swallows_Nest.jpg"
    assert info.license_short_name == "CC BY-SA 4.0"
    assert info.artist_text == "Jane Doe"
    assert info.width == 4000


def test_fetch_file_info_returns_none_for_missing_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"query": {"pages": [{"missing": True}]}})

    info = _client(httpx.MockTransport(handler)).fetch_file_info("File:Nope.jpg")
    assert info is None


def test_fetch_file_info_rejects_non_allowlisted_image_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "query": {
                    "pages": [
                        {
                            "title": "File:Evil.jpg",
                            "imageinfo": [{"url": "https://evil.example/Evil.jpg"}],
                        }
                    ]
                }
            },
        )

    info = _client(httpx.MockTransport(handler)).fetch_file_info("File:Evil.jpg")
    assert info is None


def test_download_image_enforces_host_allowlist() -> None:
    client = _client(httpx.MockTransport(lambda request: httpx.Response(200, content=b"x")))
    with pytest.raises(ValueError, match="non-allowlisted host"):
        client.download_image("https://evil.example/Evil.jpg")


def test_download_image_enforces_size_cap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 200)

    client = WikimediaCommonsClient(transport=httpx.MockTransport(handler))
    # Reach into the module constant so the test stays fast/small.
    import tourism_backend.modules.places.application.photo_import as photo_import

    original = photo_import._MAX_IMAGE_BYTES
    photo_import._MAX_IMAGE_BYTES = 50
    try:
        with pytest.raises(ValueError, match="exceeds"):
            client.download_image("https://upload.wikimedia.org/x/big.jpg")
    finally:
        photo_import._MAX_IMAGE_BYTES = original

"""Wikimedia Commons photo lookup for places (pure parsing + thin API client).

Scope (2026-08-22 slice): OSM `wikimedia_commons` / `image` tags → Wikimedia
Commons `imageinfo` API → license allowlist → caller downloads/stores.
Only ~0.1% of real OSM elements carry a direct `wikimedia_commons`/`image`
tag, so a `wikidata` tag → Wikidata P18 (image) claim → Commons title
fallback was added (2026-08-23 slice) — ~6% of Crimea candidates carry a
`wikidata` tag, a meaningfully larger yield with no new API token needed
(Wikidata's API is public, like Commons'). Mapillary is documented as a
further follow-up in `tourism-platform/docs/progress.md` (needs its own API
token; not wired here).

Security: every URL this module resolves an image from is re-validated
against `ALLOWED_HOSTS` before the caller downloads it — never follow an
arbitrary `image=` tag value off Wikimedia's own hosts. The Wikidata lookup
itself only ever queries a hardcoded `wikidata.org` API endpoint; the only
externally-controlled input (the QID) is validated against a strict
`Q[0-9]+` pattern before it reaches the request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx

#: Only these hosts may ever be treated as a photo source for this pipeline.
ALLOWED_HOSTS = frozenset({"commons.wikimedia.org", "upload.wikimedia.org"})

#: Wikimedia Commons `LicenseShortName` values safe to redistribute with
#: attribution. Fail closed: anything not matched here is rejected, not
#: merely flagged — see progress.md "лицензии!" note.
_ALLOWED_LICENSE_PATTERNS = (
    re.compile(r"^cc0", re.IGNORECASE),
    re.compile(r"^public\s*domain", re.IGNORECASE),
    re.compile(r"^pdm", re.IGNORECASE),
    re.compile(r"^cc[\s-]*by(?:[\s-]*sa)?[\s-]*[0-9.]*$", re.IGNORECASE),
)

_COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"
_WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
_USER_AGENT = "CrimeaTripPlacePhotoImport/1.0 (https://crimeatrip.example; ops@crimeatrip.example)"
_MAX_IMAGE_BYTES = 25 * 1024 * 1024
_WIKIDATA_QID_PATTERN = re.compile(r"^Q[1-9][0-9]{0,9}$")


def is_license_allowed(short_name: str | None) -> bool:
    if not short_name or not short_name.strip():
        return False
    text = short_name.strip()
    return any(pattern.match(text) for pattern in _ALLOWED_LICENSE_PATTERNS)


def normalize_wikidata_qid(raw: str | None) -> str | None:
    """Validate an OSM `wikidata` tag value as a safe-to-query QID, or None."""
    if not raw:
        return None
    candidate = raw.strip()
    return candidate if _WIKIDATA_QID_PATTERN.match(candidate) else None


def _is_allowed_host(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    return host.casefold() in ALLOWED_HOSTS


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def strip_html(raw: str, *, max_length: int = 255) -> str:
    """Commons `extmetadata` values (Artist/Credit) carry raw HTML."""
    stripper = _HTMLStripper()
    try:
        stripper.feed(raw)
    except Exception:  # noqa: BLE001 — malformed HTML must not crash the batch
        return raw.strip()[:max_length]
    return " ".join(stripper.text().split())[:max_length]


def commons_title_from_tags(tags: dict[str, str]) -> str | None:
    """Resolve a `File:...` Commons title from OSM tags, or None.

    Only `wikimedia_commons=File:...` and an `image=` tag that already
    points at an allowlisted Wikimedia host are accepted — a bare
    `image=https://random-site.example/photo.jpg` is deliberately ignored
    (see module docstring: no arbitrary URLs).
    """
    commons_tag = (tags.get("wikimedia_commons") or "").strip()
    if commons_tag.startswith("File:"):
        return commons_tag
    image_tag = (tags.get("image") or "").strip()
    if image_tag.startswith("File:"):
        return image_tag
    if image_tag.startswith("http://") or image_tag.startswith("https://"):
        if not _is_allowed_host(image_tag):
            return None
        filename = urlparse(image_tag).path.rsplit("/", 1)[-1]
        if not filename:
            return None
        return f"File:{filename}"
    return None


@dataclass(frozen=True, slots=True)
class CommonsFileInfo:
    title: str
    image_url: str
    description_url: str
    license_short_name: str | None
    artist_text: str | None
    mime: str | None
    width: int | None
    height: int | None


class WikimediaCommonsClient:
    """Thin sync client over the Commons `imageinfo` API (public, no auth)."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport

    def fetch_file_info(self, title: str) -> CommonsFileInfo | None:
        with httpx.Client(
            headers={"User-Agent": _USER_AGENT},
            timeout=self._timeout,
            transport=self._transport,
        ) as client:
            response = client.get(
                _COMMONS_API_URL,
                params={
                    "action": "query",
                    "titles": title,
                    "prop": "imageinfo",
                    "iiprop": "url|extmetadata|size|mime",
                    "format": "json",
                    "formatversion": "2",
                },
            )
            response.raise_for_status()
            payload: Any = response.json()
        return _parse_imageinfo_response(payload, title=title)

    def fetch_commons_title_via_wikidata(self, qid: str) -> str | None:
        """Resolve a Wikidata item's P18 (image) claim to a Commons `File:` title."""
        if normalize_wikidata_qid(qid) != qid:
            raise ValueError(f"Refusing to query non-QID value: {qid!r}")
        with httpx.Client(
            headers={"User-Agent": _USER_AGENT},
            timeout=self._timeout,
            transport=self._transport,
        ) as client:
            response = client.get(
                _WIKIDATA_API_URL,
                params={
                    "action": "wbgetclaims",
                    "entity": qid,
                    "property": "P18",
                    "format": "json",
                },
            )
            response.raise_for_status()
            payload: Any = response.json()
        return _parse_wikidata_p18_response(payload)

    def download_image(self, url: str) -> bytes:
        if not _is_allowed_host(url):
            raise ValueError(f"Refusing to download from non-allowlisted host: {url!r}")
        with (
            httpx.Client(
                headers={"User-Agent": _USER_AGENT},
                timeout=self._timeout,
                transport=self._transport,
            ) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > _MAX_IMAGE_BYTES:
                    raise ValueError(f"Image exceeds {_MAX_IMAGE_BYTES} bytes: {url!r}")
                chunks.append(chunk)
            return b"".join(chunks)


def _parse_imageinfo_response(payload: Any, *, title: str) -> CommonsFileInfo | None:
    if not isinstance(payload, dict):
        return None
    pages = payload.get("query", {}).get("pages")
    if not isinstance(pages, list) or not pages:
        return None
    page = pages[0]
    if not isinstance(page, dict) or page.get("missing") or page.get("invalid"):
        return None
    imageinfo = page.get("imageinfo")
    if not isinstance(imageinfo, list) or not imageinfo:
        return None
    info = imageinfo[0]
    if not isinstance(info, dict):
        return None
    image_url = info.get("url")
    description_url = info.get("descriptionurl") or image_url
    if not isinstance(image_url, str) or not _is_allowed_host(image_url):
        return None
    if not isinstance(description_url, str) or not _is_allowed_host(description_url):
        description_url = image_url

    extmetadata_raw = info.get("extmetadata")
    extmetadata: dict[str, Any] = extmetadata_raw if isinstance(extmetadata_raw, dict) else {}
    license_short_name = _extmeta_value(extmetadata, "LicenseShortName")
    artist_raw = _extmeta_value(extmetadata, "Artist")
    artist_text = strip_html(artist_raw) if artist_raw else None

    width = info.get("width")
    height = info.get("height")
    return CommonsFileInfo(
        title=str(page.get("title") or title),
        image_url=image_url,
        description_url=description_url,
        license_short_name=license_short_name,
        artist_text=artist_text,
        mime=info.get("mime") if isinstance(info.get("mime"), str) else None,
        width=width if isinstance(width, int) else None,
        height=height if isinstance(height, int) else None,
    )


def _parse_wikidata_p18_response(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    claims = payload.get("claims")
    if not isinstance(claims, dict):
        return None
    p18 = claims.get("P18")
    if not isinstance(p18, list) or not p18:
        return None
    first = p18[0]
    mainsnak = first.get("mainsnak") if isinstance(first, dict) else None
    if not isinstance(mainsnak, dict):
        return None
    datavalue = mainsnak.get("datavalue")
    if not isinstance(datavalue, dict):
        return None
    filename = datavalue.get("value")
    if not isinstance(filename, str) or not filename.strip():
        return None
    return f"File:{filename.strip()}"


def _extmeta_value(extmetadata: dict[str, Any], key: str) -> str | None:
    entry = extmetadata.get(key)
    if not isinstance(entry, dict):
        return None
    value = entry.get("value")
    return value if isinstance(value, str) and value.strip() else None

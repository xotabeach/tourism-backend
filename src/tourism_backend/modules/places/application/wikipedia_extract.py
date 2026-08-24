"""Wikipedia intro-extract lookup for places (ADR-009 P0-bis 0b.2).

Same QID-based lookup shape as `photo_import.py`: an OSM `wikidata` tag
resolves to a Russian Wikipedia article, and its plain-text intro becomes
grounding material for `content_enrichment` — real reference text instead of
the LLM inventing history it wasn't given (see `lm_studio.py`'s grounded
prompt). `scripts/fetch_wikipedia_extracts.py` is the only caller; it stores
the result under `places.source_payload["wikipedia"]`, never in a typed
column, matching ADR-009's "source_payload is the raw SoT" rule.

Security: only `www.wikidata.org` and `ru.wikipedia.org` are ever queried,
both with a QID/title resolved through this module's own lookups — never an
externally-controlled URL. The QID itself is validated by
`photo_import.normalize_wikidata_qid` before it reaches a request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from tourism_backend.modules.places.application.photo_import import normalize_wikidata_qid

_WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
_RU_WIKIPEDIA_API_URL = "https://ru.wikipedia.org/w/api.php"
_USER_AGENT = (
    "CrimeaTripPlaceContentEnrich/1.0 (https://crimeatrip.example; ops@crimeatrip.example)"
)

#: Wikipedia text is CC BY-SA 4.0 for every article — unlike Commons media,
#: there is no per-page license to read out of the API response.
WIKIPEDIA_TEXT_LICENSE = "CC BY-SA 4.0"

_SENTENCE_END_CHARS = ".!?…"
_OPEN_BRACKETS = "([«"
_CLOSE_BRACKETS = ")]»"


def _split_sentences(text: str) -> list[str]:
    """Split on `.!?…` + whitespace, except inside `(...)`/`«...»`.

    A Wikipedia lead sentence routinely opens with a parenthetical aside —
    "Ливадийский дворец (укр. Лівадійський палац) — бывшая резиденция..." —
    and "укр." is an abbreviation, not a sentence end. Bracket depth is a
    cheap, language-agnostic way to skip exactly that case without an
    abbreviation dictionary.
    """
    sentences: list[str] = []
    start = 0
    depth = 0
    i = 0
    length = len(text)
    while i < length:
        char = text[i]
        if char in _OPEN_BRACKETS:
            depth += 1
        elif char in _CLOSE_BRACKETS:
            depth = max(0, depth - 1)
        elif char in _SENTENCE_END_CHARS and depth == 0:
            end = i + 1
            while end < length and text[end] in _SENTENCE_END_CHARS:
                end += 1
            if end >= length or text[end].isspace():
                sentences.append(text[start:end].strip())
                while end < length and text[end].isspace():
                    end += 1
                start = end
                i = end
                continue
        i += 1
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def trim_extract(text: str, *, max_chars: int = 1500) -> str:
    """Cut to the last full sentence at or before max_chars, not mid-word."""
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped
    kept: list[str] = []
    length = 0
    for sentence in _split_sentences(stripped):
        addition = len(sentence) + (1 if kept else 0)
        if length + addition > max_chars:
            break
        kept.append(sentence)
        length += addition
    if kept:
        return " ".join(kept)
    return stripped[:max_chars].rstrip()


def first_sentence(text: str, *, max_chars: int = 240) -> str:
    sentences = _split_sentences(text.strip())
    candidate = sentences[0] if sentences else text.strip()
    return candidate[:max_chars].rstrip()


@dataclass(frozen=True, slots=True)
class WikipediaExtract:
    title: str
    extract: str
    page_url: str
    license: str


class WikipediaExtractClient:
    """Thin sync client over the public Wikidata/Wikipedia action APIs."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport

    def fetch_ru_title_via_wikidata(self, qid: str) -> str | None:
        """Resolve a Wikidata item's Russian Wikipedia sitelink, or None."""
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
                    "action": "wbgetentities",
                    "ids": qid,
                    "props": "sitelinks",
                    "format": "json",
                },
            )
            response.raise_for_status()
            payload: Any = response.json()
        return _parse_sitelinks_response(payload, qid=qid)

    def fetch_extract(self, title: str) -> WikipediaExtract | None:
        """Plain-text intro section of a Russian Wikipedia article."""
        with httpx.Client(
            headers={"User-Agent": _USER_AGENT},
            timeout=self._timeout,
            transport=self._transport,
        ) as client:
            response = client.get(
                _RU_WIKIPEDIA_API_URL,
                params={
                    "action": "query",
                    "titles": title,
                    "prop": "extracts",
                    "exintro": "1",
                    "explaintext": "1",
                    "redirects": "1",
                    "format": "json",
                    "formatversion": "2",
                },
            )
            response.raise_for_status()
            payload: Any = response.json()
        return _parse_extract_response(payload)


def _parse_sitelinks_response(payload: Any, *, qid: str) -> str | None:
    if not isinstance(payload, dict):
        return None
    entities = payload.get("entities")
    if not isinstance(entities, dict):
        return None
    entity = entities.get(qid)
    if not isinstance(entity, dict):
        return None
    sitelinks = entity.get("sitelinks")
    if not isinstance(sitelinks, dict):
        return None
    ruwiki = sitelinks.get("ruwiki")
    if not isinstance(ruwiki, dict):
        return None
    title = ruwiki.get("title")
    return title if isinstance(title, str) and title.strip() else None


def _parse_extract_response(payload: Any) -> WikipediaExtract | None:
    if not isinstance(payload, dict):
        return None
    pages = payload.get("query", {}).get("pages")
    if not isinstance(pages, list) or not pages:
        return None
    page = pages[0]
    if not isinstance(page, dict) or page.get("missing"):
        return None
    extract = page.get("extract")
    title = page.get("title")
    if not isinstance(extract, str) or not extract.strip():
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    page_url = "https://ru.wikipedia.org/wiki/" + title.replace(" ", "_")
    return WikipediaExtract(
        title=title,
        extract=extract.strip(),
        page_url=page_url,
        license=WIKIPEDIA_TEXT_LICENSE,
    )

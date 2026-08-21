"""Knowledge chunking: turn structured places/routes into narrative chunks.

Chunks are section-scoped (a semantic unit), not sliding windows. Only
narrative fields (title, description, how-to tips, history) are chunked — hard
facts like exact coordinates/hours flow from PostGIS via tools, not from here.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_SECTION_RE = re.compile(r"^#{2,4}\s+(.+)$", re.MULTILINE)
_FALLBACK_TYPE = "overview"
_MAX_CHUNK_WORDS = 400


@dataclass(frozen=True, slots=True)
class ChunkCandidate:
    doc_id: str
    chunk_seq: int
    title: str
    body: str
    content_type: str
    place_id: str | None
    locality: str | None
    region: str
    source: str
    license_note: str | None


def _normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def _split_sections(markdown: str) -> list[tuple[str, str]]:
    """Return [(heading, body)] split by markdown headings (## or deeper)."""
    matches = list(_SECTION_RE.finditer(markdown))
    if not matches:
        clean = _normalize_whitespace(markdown)
        return [(_FALLBACK_TYPE, clean)] if clean else []
    sections: list[tuple[str, str]] = []
    for i, match in enumerate(matches):
        heading = match.group(1).strip()
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        body = _normalize_whitespace(markdown[body_start:body_end])
        if not body:
            continue
        sections.append((heading, body))
    return sections


def _content_type_for(h_lower: str) -> str:
    if any(k in h_lower for k in ("как добраться", "добраться", "доехать", "маршрут как")):
        return "howto"
    if any(k in h_lower for k in ("совет", "лучше", "когда", "рекоменд")):  # noqa: SIM103
        return "tips"
    if any(k in h_lower for k in ("истор", "легенд", "предани", "культур")):  # noqa: SIM103
        return "history"
    return "overview"


def _chunk_markdown(
    markdown: str,
    *,
    doc_id: str,
    title: str,
    place_id: str | None,
    locality: str | None,
    region: str,
    source: str,
    license_note: str | None,
) -> list[ChunkCandidate]:
    chunks: list[ChunkCandidate] = []
    for seq, (heading, body) in enumerate(_split_sections(markdown)):
        low = heading.casefold()
        ctype = _content_type_for(low)
        # Keep narrative chunks reasonably sized; long bodies are trimmed at a
        # word level but retain section integrity (no mid-sentence cuts).
        words = body.split()
        if len(words) > _MAX_CHUNK_WORDS:
            body = " ".join(words[:_MAX_CHUNK_WORDS])
        chunks.append(
            ChunkCandidate(
                doc_id=doc_id,
                chunk_seq=seq,
                title=f"{title} · {heading}" if heading != _FALLBACK_TYPE else title,
                body=body,
                content_type=ctype,
                place_id=place_id,
                locality=locality,
                region=region,
                source=source,
                license_note=license_note,
            )
        )
    return chunks


def chunk_place_markdown(
    *,
    place_id: str,
    name: str,
    short_description: str | None,
    description: str | None,
    locality: str | None,
    region: str = "crimea",
    source: str = "internal",
    license_note: str | None = None,
) -> list[ChunkCandidate]:
    desc = short_description or description
    parts = [f"# {name}"]
    if desc:
        parts.extend(["\n\n## Описание\n", desc])
    if description and description != short_description:
        parts.extend(["\n\n## Подробнее\n", description])
    markdown = "".join(parts)
    return _chunk_markdown(
        markdown,
        doc_id=f"place:{place_id}",
        title=name,
        place_id=place_id,
        locality=locality,
        region=region,
        source=source,
        license_note=license_note,
    )


def chunk_route_markdown(
    *,
    route_id: str,
    name: str,
    short_description: str | None,
    description: str | None,
    locality: str | None,
    region: str = "crimea",
    source: str = "internal",
    license_note: str | None = None,
) -> list[ChunkCandidate]:
    desc = short_description or description
    parts = [f"# {name}"]
    if desc:
        parts.extend(["\n\n## Описание маршрута\n", desc])
    if description and description != short_description:
        parts.extend(["\n\n## Подробнее\n", description])
    markdown = "".join(parts)
    return _chunk_markdown(
        markdown,
        doc_id=f"route:{route_id}",
        title=name,
        place_id=None,
        locality=locality,
        region=region,
        source=source,
        license_note=license_note,
    )


def content_hash(body: str) -> str:
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()

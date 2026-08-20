"""Human-readable slug / description drafts for places (heuristic + optional LLM)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

_CYR_MAP = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "i",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def slugify_name(name: str, *, max_length: int = 80) -> str:
    lowered = name.strip().casefold()
    chars: list[str] = []
    for char in lowered:
        if char in _CYR_MAP:
            chars.append(_CYR_MAP[char])
        elif "a" <= char <= "z" or "0" <= char <= "9":
            chars.append(char)
        elif char in {" ", "-", "_", "/", "."}:
            chars.append("-")
    slug = re.sub(r"-{2,}", "-", "".join(chars)).strip("-")
    if not slug:
        slug = "place"
    return slug[:max_length].rstrip("-")


def stable_slug_with_suffix(name: str, source_external_id: str | None) -> str:
    base = slugify_name(name)
    if not source_external_id:
        return base
    suffix = re.sub(r"[^a-z0-9]+", "", source_external_id.casefold())[-8:]
    if not suffix:
        return base
    return f"{base}-{suffix}"[:150]


@dataclass(frozen=True, slots=True)
class ContentDraft:
    proposed_slug: str
    short_description: str
    description: str
    status: str
    provenance: dict[str, Any]


def heuristic_content_draft(
    *,
    place_id: UUID,
    name: str,
    source_external_id: str | None,
    category_names: list[str],
    city_hint: str | None = None,
) -> ContentDraft:
    categories = ", ".join(category_names[:3]) if category_names else "достопримечательность"
    where = f" в {city_hint}" if city_hint else " в Крыму"
    short = f"{name} — {categories}{where}."
    description = (
        f"{name} — туристическое место ({categories}){where}. "
        "Описание сгенерировано автоматически как черновик и требует редакционной проверки. "
        "Фактические часы работы, цены и закрытия нужно сверять с карточкой места в каталоге."
    )
    return ContentDraft(
        proposed_slug=stable_slug_with_suffix(name, source_external_id),
        short_description=short[:240],
        description=description[:2000],
        status="generated_draft",
        provenance={
            "provider": "heuristic",
            "model": None,
            "prompt_version": "heuristic-v1",
            "place_id": str(place_id),
            "source_external_id": source_external_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "content_hash": hex(hash((name, short, description)) & 0xFFFFFFFF),
        },
    )


async def llm_content_draft_or_fallback(
    *,
    place_id: UUID,
    name: str,
    source_external_id: str | None,
    category_names: list[str],
    city_hint: str | None,
    llm_enabled: bool,
    llm_callable: Any | None = None,
) -> ContentDraft:
    """Call optional LLM producer; always fall back to heuristic draft."""

    base = heuristic_content_draft(
        place_id=place_id,
        name=name,
        source_external_id=source_external_id,
        category_names=category_names,
        city_hint=city_hint,
    )
    if not llm_enabled or llm_callable is None:
        return base
    try:
        produced = await llm_callable(
            {
                "name": name,
                "categories": category_names,
                "city": city_hint,
            }
        )
    except Exception:  # noqa: BLE001 — enrichment must not fail the batch
        return base
    if not isinstance(produced, dict):
        return base
    short = str(produced.get("short_description") or base.short_description)[:240]
    description = str(produced.get("description") or base.description)[:2000]
    proposed = str(produced.get("proposed_slug") or base.proposed_slug)[:150]
    provenance = {
        **base.provenance,
        "provider": str(produced.get("provider") or "lmstudio"),
        "model": produced.get("model"),
        "prompt_version": str(produced.get("prompt_version") or "llm-v1"),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    return ContentDraft(
        proposed_slug=proposed,
        short_description=short,
        description=description,
        status="generated_draft",
        provenance=provenance,
    )

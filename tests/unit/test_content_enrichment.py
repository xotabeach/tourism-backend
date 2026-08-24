"""Unit tests for place slug/description draft generation."""

from __future__ import annotations

import pytest

from tourism_backend.modules.places.application.content_enrichment import (
    heuristic_content_draft,
    llm_content_draft_or_fallback,
)

_PLACE_ID = "9c1e3b1a-1111-4c11-8b11-000000000001"


def test_heuristic_draft_without_source_text_uses_generic_template() -> None:
    draft = heuristic_content_draft(
        place_id=_PLACE_ID,  # type: ignore[arg-type]
        name="Ханский дворец",
        source_external_id=None,
        category_names=["дворец", "музей"],
        city_hint="Бахчисарай",
    )
    assert draft.short_description == "Ханский дворец — дворец, музей в Бахчисарай."
    assert "черновик" in draft.description
    assert draft.provenance["prompt_version"] == "heuristic-v1"


def test_heuristic_draft_with_source_text_uses_real_content() -> None:
    source_text = (
        "Ханский дворец — памятник архитектуры XVI века в Бахчисарае. "
        "Резиденция крымских ханов, объект культурного наследия."
    )
    draft = heuristic_content_draft(
        place_id=_PLACE_ID,  # type: ignore[arg-type]
        name="Ханский дворец",
        source_external_id=None,
        category_names=["дворец"],
        city_hint="Бахчисарай",
        source_text=source_text,
    )
    assert draft.short_description == "Ханский дворец — памятник архитектуры XVI века в Бахчисарае."
    assert "Резиденция крымских ханов" in draft.description
    assert "По данным Wikipedia" in draft.description
    assert draft.provenance["prompt_version"] == "heuristic-wikipedia-v1"


@pytest.mark.asyncio
async def test_llm_fallback_threads_source_text_into_callable_payload() -> None:
    seen_payloads: list[dict[str, object]] = []

    async def fake_callable(payload: dict[str, object]) -> dict[str, object]:
        seen_payloads.append(payload)
        return {"short_description": "LLM short", "description": "LLM long"}

    draft = await llm_content_draft_or_fallback(
        place_id=_PLACE_ID,  # type: ignore[arg-type]
        name="Ласточкино гнездо",
        source_external_id=None,
        category_names=["замок"],
        city_hint="Гаспра",
        llm_enabled=True,
        llm_callable=fake_callable,
        source_text="Реальный текст из Wikipedia.",
    )
    assert seen_payloads[0]["source_text"] == "Реальный текст из Wikipedia."
    assert draft.short_description == "LLM short"
    assert draft.description == "LLM long"


@pytest.mark.asyncio
async def test_llm_fallback_without_callable_still_grounds_heuristic() -> None:
    draft = await llm_content_draft_or_fallback(
        place_id=_PLACE_ID,  # type: ignore[arg-type]
        name="Ласточкино гнездо",
        source_external_id=None,
        category_names=["замок"],
        city_hint="Гаспра",
        llm_enabled=False,
        llm_callable=None,
        source_text="Замок на скале, построен в 1912 году.",
    )
    assert "Замок на скале" in draft.description

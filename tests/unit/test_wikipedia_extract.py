"""Unit tests for the Wikipedia intro-extract lookup module."""

from __future__ import annotations

import httpx
import pytest

from tourism_backend.modules.places.application.wikipedia_extract import (
    WikipediaExtractClient,
    first_sentence,
    trim_extract,
)


def test_trim_extract_leaves_short_text_untouched() -> None:
    text = "Короткое описание места."
    assert trim_extract(text, max_chars=1500) == text


def test_trim_extract_cuts_at_sentence_boundary() -> None:
    text = "Первое предложение. Второе предложение. Третье совсем длинное предложение тут."
    trimmed = trim_extract(text, max_chars=45)
    assert trimmed == "Первое предложение. Второе предложение."
    assert not trimmed.endswith("Третье")


def test_trim_extract_falls_back_to_hard_cut_without_sentence_boundary() -> None:
    text = "оченьдлинноесловобезпробеловипредложений" * 3
    trimmed = trim_extract(text, max_chars=20)
    assert trimmed == text[:20]


def test_first_sentence_takes_only_the_lead() -> None:
    text = "Ханский дворец — памятник архитектуры. Построен в XVI веке крымскими ханами."
    assert first_sentence(text) == "Ханский дворец — памятник архитектуры."


def test_first_sentence_ignores_period_inside_parenthetical() -> None:
    # Real Wikipedia lead shape: a translit aside ("укр.") holds a period
    # that is not a sentence end — must not cut mid-parenthesis.
    text = "Ливадийский дворец (укр. Лівадійський палац) — бывшая резиденция. Далее текст."
    expected = "Ливадийский дворец (укр. Лівадійський палац) — бывшая резиденция."
    assert first_sentence(text) == expected


def _client(handler: httpx.MockTransport) -> WikipediaExtractClient:
    return WikipediaExtractClient(transport=handler)


def test_fetch_ru_title_via_wikidata_parses_sitelink() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.wikidata.org"
        assert request.url.params["ids"] == "Q12345"
        return httpx.Response(
            200,
            json={"entities": {"Q12345": {"sitelinks": {"ruwiki": {"title": "Ханский дворец"}}}}},
        )

    title = _client(httpx.MockTransport(handler)).fetch_ru_title_via_wikidata("Q12345")
    assert title == "Ханский дворец"


def test_fetch_ru_title_via_wikidata_returns_none_without_ruwiki_sitelink() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"entities": {"Q1": {"sitelinks": {"enwiki": {"title": "Foo"}}}}}
        )

    title = _client(httpx.MockTransport(handler)).fetch_ru_title_via_wikidata("Q1")
    assert title is None


def test_fetch_ru_title_via_wikidata_rejects_non_qid() -> None:
    client = _client(httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    with pytest.raises(ValueError, match="non-QID"):
        client.fetch_ru_title_via_wikidata("DROP TABLE places;")


def test_fetch_extract_parses_intro_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "ru.wikipedia.org"
        return httpx.Response(
            200,
            json={
                "query": {
                    "pages": [
                        {
                            "title": "Ханский дворец",
                            "extract": "Ханский дворец — памятник архитектуры XVI века.",
                        }
                    ]
                }
            },
        )

    extract = _client(httpx.MockTransport(handler)).fetch_extract("Ханский дворец")
    assert extract is not None
    assert extract.extract == "Ханский дворец — памятник архитектуры XVI века."
    assert extract.page_url == "https://ru.wikipedia.org/wiki/Ханский_дворец"
    assert extract.license == "CC BY-SA 4.0"


def test_fetch_extract_returns_none_for_missing_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"query": {"pages": [{"missing": True}]}})

    extract = _client(httpx.MockTransport(handler)).fetch_extract("Nope")
    assert extract is None

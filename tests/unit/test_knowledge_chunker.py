"""Unit tests for narrative chunking (no DB needed)."""

from tourism_backend.modules.knowledge.application.chunker import (
    _content_type_for,
    _split_sections,
    chunk_place_markdown,
    content_hash,
)


def test_split_sections_by_headings() -> None:
    md = "# Тропа Голицына\n## Описание\nТропа вдоль моря.\n## Как добраться\nИз Судака на такси.\n"
    sections = _split_sections(md)
    assert len(sections) == 2
    assert sections[0][0] == "Описание"
    assert "тропа вдоль моря" in sections[0][1].casefold()


def test_split_sections_no_headings_falls_back() -> None:
    sections = _split_sections("Просто текст без заголовков  ")
    assert len(sections) == 1
    assert sections[0][0] == "overview"
    assert sections[0][1] == "Просто текст без заголовков"


def test_content_type_detection() -> None:
    assert _content_type_for("как добраться".casefold()) == "howto"
    assert _content_type_for("Советы".casefold()) == "tips"
    assert _content_type_for("История и легенды".casefold()) == "history"
    assert _content_type_for("Обзор".casefold()) == "overview"


def test_chunk_place_markdown_produces_candidates() -> None:
    chunks = chunk_place_markdown(
        place_id="p1",
        name="Ай-Петри",
        short_description="Плато с видами.",
        description="Плато с видами.\n## Как добраться\nКанатка из Мисхора.",
        locality="Ялта",
    )
    assert len(chunks) >= 2
    assert chunks[0].doc_id == "place:p1"
    assert chunks[0].place_id == "p1"
    assert chunks[0].content_type in {"overview", "howto"}
    assert any(c.content_type == "howto" for c in chunks)
    assert all(c.locality == "Ялта" for c in chunks)


def test_chunk_place_markdown_prefers_the_full_description() -> None:
    """The full description is the actual knowledge; short_description is a
    card-length teaser for the UI and must not crowd it out or replace it.
    """
    chunks = chunk_place_markdown(
        place_id="p2",
        name="Демир-Капу",
        short_description="Скала на плато.",
        description="Скала на плато Ай-Петри, popular among climbers " * 10,
        locality="Ялта",
    )
    bodies = " ".join(c.body for c in chunks)
    assert "popular among climbers" in bodies
    assert "Скала на плато." in bodies
    # The full text is what a retriever should see first, not the teaser.
    assert bodies.index("popular among climbers") < bodies.index("Скала на плато.")


def test_chunk_place_markdown_skips_duplicate_short_description() -> None:
    chunks = chunk_place_markdown(
        place_id="p3",
        name="Ласточкино гнездо",
        short_description="Замок на скале.",
        description="Замок на скале.",
        locality="Ялта",
    )
    assert sum(c.body.count("Замок на скале.") for c in chunks) == 1


def test_content_hash_stable() -> None:
    assert content_hash("x") == content_hash("x")
    assert content_hash("x") != content_hash("y")

"""Nightly reconciliation of the knowledge index (spec 18, D13)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tourism_backend.config import Settings
from tourism_backend.modules.knowledge.application.chunker import ChunkCandidate, content_hash
from tourism_backend.modules.knowledge.application.reindex_job import (
    IndexedChunk,
    plan_reindex,
    poll_knowledge_reindex,
    seconds_until,
)

MODEL = "paraphrase-multilingual-MiniLM-L12-v2"


def _candidate(doc_id: str, seq: int, body: str) -> ChunkCandidate:
    return ChunkCandidate(
        doc_id=doc_id,
        chunk_seq=seq,
        title="t",
        body=body,
        content_type="overview",
        place_id=None,
        locality=None,
        region="crimea",
        source="internal",
        license_note=None,
    )


def _indexed(body: str, *, model: str | None = MODEL, embedded: bool = True) -> IndexedChunk:
    return IndexedChunk(uuid4(), content_hash(body), model, embedded)


def test_untouched_chunk_costs_nothing() -> None:
    plan = plan_reindex(
        {("place:1", 0): _indexed("same")}, [_candidate("place:1", 0, "same")], model=MODEL
    )
    assert (plan.insert, plan.update, plan.embed, plan.stale) == ([], [], [], [])


def test_new_and_changed_texts_are_written() -> None:
    old = _indexed("old text")
    plan = plan_reindex(
        {("route:1", 0): old},
        [_candidate("route:1", 0, "new text"), _candidate("route:2", 0, "fresh")],
        model=MODEL,
    )
    assert [c.doc_id for c in plan.insert] == ["route:2"]
    assert [(chunk_id, c.body) for chunk_id, c in plan.update] == [(old.id, "new text")]
    assert plan.embed == []
    assert plan.stale == []


def test_vector_of_another_model_or_none_is_embedded_again() -> None:
    """A vector of another model is never compared with a query: the chunk
    would stay in the index and be invisible to the search (2026-10-08, the
    whole production index was hash-v1 under a MiniLM query)."""
    stub = _indexed("a", model="hash-v1")
    bare = _indexed("b", embedded=False)
    plan = plan_reindex(
        {("place:1", 0): stub, ("place:2", 0): bare},
        [_candidate("place:1", 0, "a"), _candidate("place:2", 0, "b")],
        model=MODEL,
    )
    assert {chunk_id for chunk_id, _ in plan.embed} == {stub.id, bare.id}
    assert plan.update == []


def test_what_is_no_longer_published_leaves_the_index() -> None:
    archived = _indexed("old route")
    leftover = _indexed("third part")
    plan = plan_reindex(
        {
            ("route:gone", 0): archived,
            ("place:1", 0): _indexed("a"),
            ("place:1", 2): leftover,
        },
        [_candidate("place:1", 0, "a")],
        model=MODEL,
    )
    assert set(plan.stale) == {archived.id, leftover.id}


def test_next_run_is_the_coming_night_in_moscow() -> None:
    # 00:10 UTC is 03:10 in Moscow: twenty minutes to 03:30.
    assert seconds_until(3, 30, now=datetime(2026, 10, 8, 0, 10, tzinfo=UTC)) == 20 * 60
    # 00:30 UTC is 03:30 sharp: the next run is tomorrow, not now again.
    assert seconds_until(3, 30, now=datetime(2026, 10, 8, 0, 30, tzinfo=UTC)) == 24 * 3600
    # Midday: the coming night.
    assert seconds_until(3, 30, now=datetime(2026, 10, 8, 9, 30, tzinfo=UTC)) == 15 * 3600


@pytest.mark.parametrize("fields", [{"rag_enabled": False}, {"rag_reindex_enabled": False}])
def test_job_does_not_start_when_switched_off(fields: dict[str, bool]) -> None:
    settings = Settings(**{"rag_enabled": True, "rag_reindex_enabled": True, **fields})
    # Returns at once instead of sleeping until the night.
    asyncio.run(asyncio.wait_for(poll_knowledge_reindex(None, settings), timeout=1))  # type: ignore[arg-type]

import asyncio
import math
from uuid import UUID

import pytest

from tourism_backend.modules.support.application.help_semantic import (
    MINILM_MODEL,
    HelpQueryEncoder,
    fuse_rankings,
    help_query_encoder,
    normalized_vector,
    search_documents,
    semantic_ranking,
)


def vector(first: float = 1.0, second: float = 0.0) -> list[float]:
    return [first, second] + [0.0] * 382


@pytest.mark.parametrize("values", [[], [1.0], vector(math.nan), vector(math.inf), vector(0)])
def test_invalid_vectors_are_rejected(values: list[float]) -> None:
    with pytest.raises(ValueError, match="Invalid help embedding"):
        normalized_vector(values)


def test_semantic_threshold_deduplication_and_corruption() -> None:
    first, second, third = UUID(int=1), UUID(int=2), UUID(int=3)
    assert semantic_ranking(
        vector(),
        [(first, vector()), (first, vector(0.9, 0.1)), (second, vector(0, 1)), (third, [])],
        min_score=0.6,
    ) == [first]


def test_rank_fusion_does_not_mix_fts_and_cosine_scores() -> None:
    a, b, c, d = [UUID(int=i) for i in range(1, 5)]
    assert fuse_rankings([a, b, c], [b, d]) == [b, a, d]
    assert fuse_rankings([], [d, d, b]) == [d, b]
    assert fuse_rankings([a, b], []) == [a, b]


def test_search_documents_include_body_with_topic_not_generic_headings() -> None:
    paragraph = "Подробное описание того, что пользователь может проверить перед обращением."
    docs = search_documents("Вход", "Как войти?", f"Что дальше\n\n{paragraph}")
    assert docs == ["Вход. Как войти?", f"Вход. {paragraph}"]


def test_hash_and_unverified_models_never_become_semantic_search() -> None:
    assert help_query_encoder("hash-v1", 1.5) is None
    assert help_query_encoder("a-different-384d-model", 1.5) is None


class SlowProvider:
    model_id = MINILM_MODEL

    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def warm(self) -> None:
        pass

    async def embed(self, query: str) -> list[float]:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return vector()


class QueueingProvider:
    """Serves one embed at a time, on demand, counting overlap."""

    model_id = MINILM_MODEL

    def __init__(self) -> None:
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0

    async def warm(self) -> None:
        pass

    async def embed(self, query: str) -> list[float]:
        self.calls += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            await asyncio.sleep(0)  # Yield, so an overlapping caller could run.
            return vector()
        finally:
            self.concurrent -= 1


async def test_everyone_asking_at_once_still_gets_the_model() -> None:
    """Overlapping questions queue for MiniLM instead of being downgraded.

    The first version answered one caller from the model and handed every
    other concurrent caller back to lexical search — two people typing at the
    same moment got different answer quality for no visible reason.
    """
    provider = QueueingProvider()
    encoder = HelpQueryEncoder(provider, timeout_seconds=5, queue_seconds=5)

    results = await asyncio.gather(*(encoder.encode(f"вопрос {i}") for i in range(5)))

    assert all(result == vector() for result in results)
    assert provider.calls == 5
    # Queued, not parallel: the CPU slot is still one at a time.
    assert provider.max_concurrent == 1
    assert encoder.waiting == 0


async def test_a_queue_too_long_to_serve_answers_from_lexical_search() -> None:
    """The queue is bounded: past the cap, waiting can no longer pay off."""
    provider = SlowProvider()
    encoder = HelpQueryEncoder(provider, timeout_seconds=5, queue_seconds=5, max_waiting=2)
    first = asyncio.create_task(encoder.encode("первый"))
    await provider.started.wait()
    second = asyncio.create_task(encoder.encode("второй"))
    await asyncio.sleep(0)

    # Two are in the system; the third is turned away immediately rather than
    # joining a queue nobody can serve in time.
    assert await encoder.encode("третий") is None
    assert provider.calls == 1

    provider.release.set()
    assert await first == vector()
    assert await second == vector()
    assert encoder.waiting == 0


async def test_waiting_longer_than_the_budget_falls_back_without_queueing_forever() -> None:
    provider = SlowProvider()
    encoder = HelpQueryEncoder(provider, timeout_seconds=5, queue_seconds=0.01)
    first = asyncio.create_task(encoder.encode("первый"))
    await provider.started.wait()

    # The slot is busy for longer than this caller may wait.
    assert await encoder.encode("второй") is None
    assert provider.calls == 1

    provider.release.set()
    assert await first == vector()


async def test_a_timed_out_caller_does_not_hand_the_slot_over_early() -> None:
    """A cancelled `to_thread` await does not stop torch.

    Releasing the slot when the waiter gives up would start a second encode
    while the first still burns CPU — the thing the single slot exists to
    prevent.
    """
    provider = SlowProvider()
    encoder = HelpQueryEncoder(provider, timeout_seconds=0.01, queue_seconds=0.01)

    assert await encoder.encode("первый") is None  # Times out, work continues.
    assert await encoder.encode("второй") is None  # Slot still held by the first.
    assert provider.calls == 1

    provider.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert await encoder.encode("третий") == vector()
    assert provider.calls == 2


async def test_cancelled_request_does_not_release_cpu_slot_early() -> None:
    provider = SlowProvider()
    encoder = HelpQueryEncoder(provider, queue_seconds=0.01)
    request = asyncio.create_task(encoder.encode("частный вопрос"))
    await provider.started.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert await encoder.encode("другой вопрос") is None
    assert provider.calls == 1
    provider.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_provider_error_falls_back_without_logging_private_question(caplog) -> None:
    class BrokenProvider(SlowProvider):
        async def embed(self, query: str) -> list[float]:
            raise RuntimeError(query)

    assert await HelpQueryEncoder(BrokenProvider()).encode("личный телефон и вопрос") is None
    assert "личный телефон" not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_long_source_is_windowed_before_encoding_without_optional_dependencies(monkeypatch):
    from tourism_backend.modules.knowledge.application import embedder as module

    class Tokenizer:
        @staticmethod
        def num_special_tokens_to_add():
            return 2

        @staticmethod
        def encode(text, add_special_tokens=True):
            tokens = [ord(char) for char in text]
            return [0, *tokens, 0] if add_special_tokens else tokens

        @staticmethod
        def decode(tokens):
            return "".join(chr(token) for token in tokens)

    class Model:
        tokenizer = Tokenizer()
        max_seq_length = 32
        windows: list[str] = []

        def encode(self, windows, normalize_embeddings):
            assert normalize_embeddings
            assert all(
                len(self.tokenizer.encode(window)) <= self.max_seq_length for window in windows
            )
            self.windows = windows
            return [vector() for _ in windows]

    model = Model()

    async def loaded(_name):
        return model

    monkeypatch.setattr(module, "_load_off_loop", loaded)
    provider = module.SentenceTransformerEmbeddingProvider(model_name=MINILM_MODEL)
    source = "abcdefghijklmnopqrstuvwxyz0123456789"
    result = await provider.embed_passages([source])
    assert len(result) > 1
    assert set("".join(model.windows)) == set(source)
    assert model.windows[0].startswith(source[:8])
    assert model.windows[-1].endswith(source[-8:])

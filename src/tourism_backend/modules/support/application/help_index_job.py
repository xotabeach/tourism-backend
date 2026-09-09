"""Runs the help index from the admin without blocking the request.

Indexing loads MiniLM and embeds every published article, which is seconds
to a minute of CPU. Doing that inside the admin request would hold a worker
and time the browser out, so the run happens in the background and this
module is what the page reads to say how it went.

State is in-process and deliberately so: it describes one run of one backend
process, and after a restart there is nothing honest to report. `status`
says `unknown` then, rather than implying the last run is still current.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from tourism_backend.modules.knowledge.application.embedder import (
    SentenceTransformerEmbeddingProvider,
)
from tourism_backend.modules.support.application.help_semantic import (
    MINILM_ALIASES,
    index_help,
)

logger = logging.getLogger(__name__)

RunStatus = Literal["unknown", "running", "done", "failed"]


@dataclass(frozen=True)
class HelpIndexRun:
    status: RunStatus
    app_version: str | None = None
    actor: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    changed: int | None = None
    error: str | None = None

    @property
    def is_running(self) -> bool:
        return self.status == "running"


class HelpIndexJob:
    """One run at a time, per process."""

    def __init__(self) -> None:
        self._run = HelpIndexRun(status="unknown")
        self._task: asyncio.Task[None] | None = None

    @property
    def last_run(self) -> HelpIndexRun:
        return self._run

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(
        self,
        *,
        session_maker: Callable[..., Any],
        app_version: str,
        model_id: str,
        actor: str | None,
    ) -> bool:
        """Kicks off a run. Returns False when one is already going."""
        if self.busy:
            return False
        if model_id not in MINILM_ALIASES:
            self._run = HelpIndexRun(
                status="failed",
                app_version=app_version,
                actor=actor,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                error=(
                    "Индексация возможна только на multilingual MiniLM. "
                    f"Сейчас настроено: {model_id}."
                ),
            )
            return False
        self._run = HelpIndexRun(
            status="running",
            app_version=app_version,
            actor=actor,
            started_at=datetime.now(UTC),
        )
        self._task = asyncio.create_task(
            self._run_job(
                session_maker=session_maker,
                app_version=app_version,
                model_id=model_id,
                actor=actor,
            )
        )
        return True

    async def _run_job(
        self,
        *,
        session_maker: Callable[..., Any],
        app_version: str,
        model_id: str,
        actor: str | None,
    ) -> None:
        started = self._run.started_at or datetime.now(UTC)
        try:
            provider = SentenceTransformerEmbeddingProvider(model_name=model_id)
            async with session_maker(expire_on_commit=False) as session, session.begin():
                changed = await index_help(session, app_version=app_version, provider=provider)
            self._run = HelpIndexRun(
                status="done",
                app_version=app_version,
                actor=actor,
                started_at=started,
                finished_at=datetime.now(UTC),
                changed=changed,
            )
        except Exception as exc:  # noqa: BLE001 — the page reports it, nothing raises here
            logger.warning("support_help_index_job_failed", exc_info=True)
            self._run = HelpIndexRun(
                status="failed",
                app_version=app_version,
                actor=actor,
                started_at=started,
                finished_at=datetime.now(UTC),
                error=f"{type(exc).__name__}: {exc}"[:500],
            )


@dataclass(frozen=True)
class IndexCoverage:
    """How much of what is published is actually searchable semantically."""

    app_version: str
    published: int
    indexed: int
    stale: int

    @property
    def complete(self) -> bool:
        return self.published > 0 and self.indexed == self.published and self.stale == 0


async def index_coverage(session: Any, *, app_version: str) -> IndexCoverage:
    """Published articles, and how many have a usable index entry.

    "Stale" is an article whose text changed after it was indexed: search
    matches embeddings on `content_hash`, so those simply stop contributing
    semantic hits — silently, which is why the number is worth showing.
    """
    from sqlalchemy import distinct, func, select

    from tourism_backend.modules.support.application.help_visibility import visible_help
    from tourism_backend.modules.support.infrastructure.help_models import (
        SupportHelpEmbedding,
        SupportHelpRevision,
    )

    published = int(
        await session.scalar(
            select(func.count()).select_from(SupportHelpRevision).where(*visible_help(app_version))
        )
        or 0
    )
    fresh = int(
        await session.scalar(
            select(func.count(distinct(SupportHelpRevision.id)))
            .select_from(SupportHelpRevision)
            .join(
                SupportHelpEmbedding,
                SupportHelpEmbedding.revision_id == SupportHelpRevision.id,
            )
            .where(
                *visible_help(app_version),
                SupportHelpEmbedding.content_hash == SupportHelpRevision.content_hash,
            )
        )
        or 0
    )
    any_entry = int(
        await session.scalar(
            select(func.count(distinct(SupportHelpRevision.id)))
            .select_from(SupportHelpRevision)
            .join(
                SupportHelpEmbedding,
                SupportHelpEmbedding.revision_id == SupportHelpRevision.id,
            )
            .where(*visible_help(app_version))
        )
        or 0
    )
    return IndexCoverage(
        app_version=app_version,
        published=published,
        indexed=fresh,
        stale=max(0, any_entry - fresh),
    )


help_index_job = HelpIndexJob()

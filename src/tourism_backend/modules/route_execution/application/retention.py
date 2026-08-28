"""Safe retention helpers for immutable routing snapshots.

Snapshots are cheap compared with a route run's audit value, so retention is
conservative by design: a row is eligible only when it is older than the
configured cutoff, is not referenced by an execution, and is not the newest
revision for its route.  Deletion is explicit maintenance work and never runs
from a request handler.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Select, delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteExecution,
    RouteRoutingSnapshot,
)

DEFAULT_RETENTION_DAYS = 365
MAX_RETENTION_DAYS = 3_650
DEFAULT_BATCH_SIZE = 500
MAX_BATCH_SIZE = 5_000
MAX_BATCHES_PER_RUN = 20


@dataclass(frozen=True, slots=True)
class SnapshotRetentionResult:
    """Bounded maintenance outcome suitable for a job log/metric."""

    scanned: int
    deleted: int
    batches: int
    dry_run: bool


def retention_cutoff(
    retention_days: int = DEFAULT_RETENTION_DAYS,
    *,
    now: datetime | None = None,
) -> datetime:
    """Return an aware UTC cutoff and reject unsafe operator input."""

    if not 1 <= retention_days <= MAX_RETENTION_DAYS:
        raise ValueError(f"retention_days must be between 1 and {MAX_RETENTION_DAYS}")
    current = now or datetime.now(UTC)
    current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
    return current - timedelta(days=retention_days)


def eligible_snapshot_ids(
    *,
    cutoff: datetime,
    limit: int = DEFAULT_BATCH_SIZE,
) -> Select[tuple[UUID]]:
    """Build a bounded query for snapshots that may safely be removed.

    ``RouteExecution.routing_snapshot_id`` is the authoritative reference. A
    separate ``newer`` predicate keeps the latest route revision even when no
    execution currently points at it, so starting a route later never loses
    its baseline.
    """

    if not 1 <= limit <= MAX_BATCH_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_BATCH_SIZE}")
    newer = aliased(RouteRoutingSnapshot)
    referenced = exists(
        select(1).where(RouteExecution.routing_snapshot_id == RouteRoutingSnapshot.id)
    )
    has_newer = exists(
        select(1).where(
            newer.route_id == RouteRoutingSnapshot.route_id,
            newer.revision > RouteRoutingSnapshot.revision,
        )
    )
    return (
        select(RouteRoutingSnapshot.id)
        .where(
            RouteRoutingSnapshot.created_at < cutoff,
            ~referenced,
            ~has_newer,
        )
        .order_by(RouteRoutingSnapshot.created_at, RouteRoutingSnapshot.id)
        .limit(limit)
    )


async def purge_routing_snapshots(
    session: AsyncSession,
    *,
    cutoff: datetime,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = MAX_BATCHES_PER_RUN,
    dry_run: bool = False,
) -> SnapshotRetentionResult:
    """Delete only unreferenced, non-latest snapshots in bounded batches.

    The function deliberately does not commit: a maintenance command can
    choose its transaction boundary and observability policy.  ``dry_run``
    still performs one bounded scan and never mutates the database.
    """

    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    if not 1 <= max_batches <= MAX_BATCHES_PER_RUN:
        raise ValueError(f"max_batches must be between 1 and {MAX_BATCHES_PER_RUN}")
    cutoff = cutoff.replace(tzinfo=UTC) if cutoff.tzinfo is None else cutoff.astimezone(UTC)

    scanned = 0
    deleted = 0
    batches = 0
    for _ in range(max_batches):
        ids = list(
            (await session.scalars(eligible_snapshot_ids(cutoff=cutoff, limit=batch_size))).all()
        )
        batches += 1
        scanned += len(ids)
        if not ids or dry_run:
            break
        await session.execute(delete(RouteRoutingSnapshot).where(RouteRoutingSnapshot.id.in_(ids)))
        # Every selected id is protected by the same predicates in the
        # transaction, so a successful DELETE removes exactly this batch.
        deleted += len(ids)
        await session.flush()
        if len(ids) < batch_size:
            break
    return SnapshotRetentionResult(
        scanned=scanned,
        deleted=deleted,
        batches=batches,
        dry_run=dry_run,
    )


__all__: Sequence[str] = (
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_RETENTION_DAYS",
    "MAX_BATCHES_PER_RUN",
    "MAX_BATCH_SIZE",
    "MAX_RETENTION_DAYS",
    "SnapshotRetentionResult",
    "eligible_snapshot_ids",
    "purge_routing_snapshots",
    "retention_cutoff",
)

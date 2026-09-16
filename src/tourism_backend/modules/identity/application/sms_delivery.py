"""Durable OTP SMS delivery with immediate dispatch and a restart-safe poller."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tourism_backend.config import Settings
from tourism_backend.modules.identity.infrastructure.models import SmsDeliveryJob
from tourism_backend.modules.identity.infrastructure.sms_provider import (
    SmsAeroProvider,
    SmsProvider,
    SmsProviderError,
    StubSmsProvider,
)
from tourism_backend.modules.runtime_config.application.service import effective_sms_settings

_logger = logging.getLogger("tourism_backend.sms_delivery")
_POLL_LOCK_KEY = "auth:sms:poller"
_POLL_LOCK_TTL_SECONDS = 60
_BATCH_SIZE = 50
_STALE_SENDING_AFTER = timedelta(minutes=2)
_FAILURE_ALERT_WINDOW = timedelta(minutes=15)
_RETRY_DELAYS = (0.5, 2.0, 5.0)
_tasks: set[asyncio.Task[None]] = set()
_RELEASE_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


def new_delivery_job(
    *,
    phone_e164: str,
    plaintext_code: str,
    otp_challenge_id: UUID | None = None,
    phone_change_challenge_id: UUID | None = None,
) -> SmsDeliveryJob:
    now = datetime.now(UTC)
    return SmsDeliveryJob(
        id=uuid4(),
        phone_e164=phone_e164,
        plaintext_code=plaintext_code,
        otp_challenge_id=otp_challenge_id,
        phone_change_challenge_id=phone_change_challenge_id,
        status="pending",
        attempts=0,
        next_attempt_at=now,
        provider_sms_id=None,
        last_error=None,
        created_at=now,
        updated_at=now,
    )


def schedule_delivery(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    job_id: UUID,
) -> None:
    """Start the happy-path send using a new session after the caller commits."""
    bind = getattr(session, "bind", None)
    if not isinstance(bind, AsyncEngine):
        # Hand-written unit-test sessions and unusual connection-bound sessions
        # still retain the durable job for the poller.
        return
    factory = async_sessionmaker(bind, expire_on_commit=False)
    task = asyncio.create_task(process_job(factory, redis, settings, job_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _provider(settings: Settings, provider_name: str) -> SmsProvider:
    if provider_name == "stub":
        return StubSmsProvider()
    if provider_name != "smsaero":
        raise SmsProviderError("unsupported SMS provider")
    key = settings.smsaero_api_key
    email = settings.smsaero_email
    if key is None or not key.get_secret_value().strip() or not email:
        raise SmsProviderError("SMS Aero credentials are not configured")
    return SmsAeroProvider(
        email=email,
        api_key=key.get_secret_value(),
        base_url=settings.smsaero_base_url,
        timeout_seconds=settings.smsaero_timeout_seconds,
    )


async def _claim_job(session: AsyncSession, job_id: UUID, max_attempts: int) -> bool:
    now = datetime.now(UTC)
    claimed = await session.scalar(
        update(SmsDeliveryJob)
        .where(
            SmsDeliveryJob.id == job_id,
            SmsDeliveryJob.status == "pending",
            SmsDeliveryJob.attempts < max_attempts,
            SmsDeliveryJob.next_attempt_at <= now,
        )
        .values(
            status="sending",
            attempts=SmsDeliveryJob.attempts + 1,
            updated_at=now,
        )
        .returning(SmsDeliveryJob.id)
    )
    await session.commit()
    return claimed is not None


async def _record_daily_send(redis: Redis, budget: int) -> None:
    now = datetime.now(UTC)
    key = f"auth:sms:daily:{now:%Y-%m-%d}"
    count = await redis.incr(key)
    if count == 1:
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        await redis.expire(key, max(1, int((tomorrow - now).total_seconds())))
    if count == budget + 1:
        _logger.warning("sms_daily_soft_budget_exceeded", extra={"count": count, "budget": budget})


def _safe_error(exc: Exception) -> str:
    # Provider exceptions are deliberately credential/phone/code-free.
    return str(exc)[:500] or exc.__class__.__name__


async def process_job(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    settings: Settings,
    job_id: UUID,
) -> None:
    async with session_factory() as session:
        if not await _claim_job(session, job_id, settings.sms_max_attempts):
            return
        job = await session.get(SmsDeliveryJob, job_id)
        if job is None:
            return
        if job.plaintext_code is None:
            job.status = "failed"
            job.last_error = "SMS job has no plaintext code"
            job.next_attempt_at = None
            job.updated_at = datetime.now(UTC)
            await session.commit()
            return
        runtime = await effective_sms_settings(session, settings)
        try:
            text = runtime.template.format(code=job.plaintext_code)
            provider = _provider(settings, runtime.provider)
            if runtime.provider == "smsaero":
                await _record_daily_send(redis, settings.sms_daily_soft_budget)
            result = await provider.send(
                phone_e164=job.phone_e164,
                text=text,
                sender=runtime.sender,
            )
        except Exception as exc:  # noqa: BLE001 — a job records every provider failure
            job.last_error = _safe_error(exc)
            is_ambiguous = isinstance(exc, SmsProviderError) and exc.ambiguous
            if is_ambiguous or job.attempts >= settings.sms_max_attempts:
                job.status = "failed"
                job.plaintext_code = None
                job.next_attempt_at = None
            else:
                delay_index = min(job.attempts - 1, len(_RETRY_DELAYS) - 1)
                job.status = "pending"
                job.next_attempt_at = datetime.now(UTC) + timedelta(
                    seconds=_RETRY_DELAYS[delay_index]
                )
            job.updated_at = datetime.now(UTC)
            await session.commit()
            _logger.warning(
                "sms_delivery_failed",
                extra={
                    "job_id": str(job.id),
                    "attempts": job.attempts,
                    "terminal": job.status == "failed",
                },
            )
            return

        job.status = "sent"
        job.provider_sms_id = result.provider_sms_id
        job.last_error = None
        job.next_attempt_at = None
        job.plaintext_code = None
        job.updated_at = datetime.now(UTC)
        await session.commit()


async def _recover_stale_claims(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    await session.execute(
        update(SmsDeliveryJob)
        .where(
            SmsDeliveryJob.status == "sending",
            SmsDeliveryJob.updated_at < now - _STALE_SENDING_AFTER,
        )
        # Once a worker claimed the job, a crash leaves the external outcome
        # ambiguous. Retrying could send a second billed OTP, so fail closed.
        .values(
            status="failed",
            plaintext_code=None,
            next_attempt_at=None,
            last_error="delivery worker stopped with an ambiguous outcome",
            updated_at=now,
        )
    )
    await session.commit()


async def _eligible_job_ids(session: AsyncSession, *, max_attempts: int) -> list[UUID]:
    rows = await session.scalars(
        select(SmsDeliveryJob.id)
        .where(
            SmsDeliveryJob.status == "pending",
            SmsDeliveryJob.attempts < max_attempts,
            SmsDeliveryJob.next_attempt_at <= datetime.now(UTC),
        )
        .order_by(SmsDeliveryJob.next_attempt_at, SmsDeliveryJob.created_at)
        .limit(_BATCH_SIZE)
    )
    return list(rows)


async def _warn_on_failure_ratio(session: AsyncSession) -> None:
    since = datetime.now(UTC) - _FAILURE_ALERT_WINDOW
    total = await session.scalar(
        select(func.count()).select_from(SmsDeliveryJob).where(SmsDeliveryJob.created_at >= since)
    )
    failed = await session.scalar(
        select(func.count())
        .select_from(SmsDeliveryJob)
        .where(SmsDeliveryJob.created_at >= since, SmsDeliveryJob.status == "failed")
    )
    total_count = int(total or 0)
    failed_count = int(failed or 0)
    if total_count >= 4 and failed_count / total_count > 0.5:
        _logger.warning(
            "sms_delivery_failure_ratio_high",
            extra={"failed": failed_count, "total": total_count, "window_minutes": 15},
        )


async def sweep_once(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings
) -> None:
    lock_token = str(uuid4())
    acquired = await redis.set(_POLL_LOCK_KEY, lock_token, nx=True, ex=_POLL_LOCK_TTL_SECONDS)
    if not acquired:
        return
    try:
        async with session_factory() as session:
            await _recover_stale_claims(session)
            ids = await _eligible_job_ids(session, max_attempts=settings.sms_max_attempts)
        for job_id in ids:
            await process_job(session_factory, redis, settings, job_id)
        async with session_factory() as session:
            await _warn_on_failure_ratio(session)
    finally:
        await redis.eval(_RELEASE_LOCK_SCRIPT, 1, _POLL_LOCK_KEY, lock_token)


async def poll_delivery_jobs(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings
) -> None:
    while True:
        try:
            await sweep_once(session_factory, redis, settings)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a failed sweep must not stop future delivery
            _logger.exception("sms_delivery_sweep_failed")
        await asyncio.sleep(settings.sms_poll_interval_seconds)


async def stop_delivery_poller(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def stop_immediate_deliveries() -> None:
    pending = list(_tasks)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

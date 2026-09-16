# SMS Aero OTP Delivery Integration

> Provider amendment (2026-09-16): the interview originally selected SMSC.ru,
> but implementation uses SMS Aero API v2 at `https://gate.smsaero.ru/v2`.
> SMSC-specific transport assumptions in the interview artifact are historical;
> this document and the implementation below are authoritative.

## Overview / Problem Statement

Before this implementation, OTP codes for login/registration (`AuthOtpChallenge`) and phone-number change (`AuthPhoneChangeChallenge`) were generated in `identity/application/service.py` but were **never actually delivered to the user**. Phone-based auth was therefore non-functional outside local/test debug mode.

This spec integrates [SMS Aero API v2](https://smsaero.ru/integration/documentation/api/) as the real delivery mechanism, replacing the TODO with a working, production-grade send path. Codes continue to be generated backend-side as today; the admin panel continues to show challenges as it already does (`OtpChallengeAdmin`, `PhoneChangeChallengeAdmin` in `modules/admin/presentation/views.py`). What's new is: (1) a persisted delivery job created atomically with each code, and (2) the SMS Aero HTTP client that sends it.

This is the **first real, working phone-authentication flow** this codebase will have running in production.

## Goals

- Deliver OTP codes to real phones via SMS Aero, using account email + API key.
- Give ops visibility into delivery outcomes via the existing SQLAdmin panel (view-only).
- Survive a process crash/restart without silently losing a queued send (this codebase has no external job queue — the solution has to work as a single in-process FastAPI worker).
- Protect against runaway SMS spend and against hammering a degraded SMS Aero gateway.
- Deploy safely with the stub as the default, then activate SMS Aero explicitly after credentials and sender approval are in place.

## Non-Goals

- SMS Aero delivery-report callbacks (confirmed-to-handset delivery status). "Sent" means the gateway accepted the request, not confirmed delivery.
- A general-purpose job queue/broker for the rest of the codebase (Kafka etc. remain explicitly deferred per ADR-005). This job table is scoped to SMS delivery only.
- Manual "retry now" admin action (the 5-minute OTP TTL makes a human-triggered retry rarely useful before the code is already dead).
- Retiring `AUTH_OTP_ACCEPT_ANY` / `AUTH_OTP_STORE_DEBUG_CODE` — they stay as local/test conveniences.

## Architecture: Job Creation & Delivery

### Flow

1. `request_otp` / `request_phone_change` generates the code and upserts the challenge row as today (`AuthOtpChallenge` or `AuthPhoneChangeChallenge`), now with **TTL = 5 minutes** (reduced from 10 — see Decisions Log D4).
2. In the same request, a `SmsDeliveryJob` row is inserted (`status="pending"`, plaintext code, phone, FK to whichever challenge type applies) and committed.
3. Immediately after commit, an `asyncio.create_task` is dispatched to attempt the send right away (fast path for the happy case).
4. A background poller loop, started once at FastAPI `lifespan` startup, periodically sweeps for jobs that still need sending — this is the safety net if the process crashes between the commit and the immediate-dispatch task, or if the immediate attempt itself fails and needs a retry.

### Poller design (fixed in spec per red-team finding — no existing periodic-loop precedent to copy)

There is **no existing example** of a periodic in-process background loop anywhere in this codebase — `HelpIndexJob` is a manual one-shot trigger, not a scheduled loop, and D2 originally cited it as a precedent incorrectly. The poller must therefore be designed explicitly:

- Runs as a loop started in `main.py`'s `_lifespan`, sleeping a fixed interval between sweeps (recommended: 20s — frequent enough that the safety-net path only adds single-digit seconds of latency within the 5-minute TTL, infrequent enough not to hammer Postgres).
- **Overlap protection**: a single-flight Redis lock (`SET NX EX`, same idiom as the existing `auth:otp:issue:{phone}` lock) held for the duration of one sweep, so a slow sweep can't overlap with the next tick.
- **Row claiming**: both the immediate-trigger path and the poller claim a job via an atomic `UPDATE sms_delivery_jobs SET status='sending' WHERE id=:id AND status='pending'` (returning whether a row was updated) before attempting to send — this is the single point of concurrency control that prevents the immediate-trigger and poller from ever sending the same job twice (D26).
- **No challenge/job orphan window**: the challenge and its delivery job are inserted in the same database transaction. The immediate task starts only after that transaction commits; if the process stops before dispatch, the durable pending job is picked up by the poller.
- Also owns the retry loop: any job in `status='pending'` with `attempts < max_attempts` and `next_attempt_at <= now()` is eligible for the poller to claim and retry.

## Data Model

### `SmsDeliveryJob` (new table, new Alembic migration `0059_sms_delivery_jobs.py`)

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `phone_e164` | text | denormalized from the challenge, so the sender doesn't need a join |
| `plaintext_code` | text, nullable | the code to send; **NULLed once the job reaches a terminal status** (`sent`/`failed`) — see Security |
| `otp_challenge_id` | UUID, nullable FK → `auth_otp_challenges.id` | |
| `phone_change_challenge_id` | UUID, nullable FK → `auth_phone_change_challenges.id` | |
| `status` | text | `pending` \| `sending` \| `sent` \| `failed`, `CheckConstraint` |
| `attempts` | int, default 0 | |
| `next_attempt_at` | timestamptz, nullable | poller retry scheduling |
| `provider_sms_id` | text, nullable | SMS Aero's returned `data.id`, for support lookups in its dashboard |
| `last_error` | text, nullable | sanitized error from SMS Aero or the HTTP layer |
| `created_at` / `updated_at` | timestamptz | |

**DB-level invariant (D21, red-team finding)**: a `CheckConstraint` enforces that **exactly one** of `otp_challenge_id` / `phone_change_challenge_id` is non-null — matching this codebase's existing convention for this shape of either/or invariant (see `content_reports`' constraints). Without this, an application bug could silently create an orphaned or ambiguous row.

### OTP TTL change (D4)

`_OTP_TTL` in `identity/application/service.py` changes from 10 minutes to **5 minutes**, applied uniformly to both `AuthOtpChallenge` and `AuthPhoneChangeChallenge` (they share the constant today). This directly bounds how long a user waits if their SMS job permanently fails and they must wait for natural expiry before a new challenge+job is created (see Resend UX below).

## Resend / Failure UX

`request_otp` (and the phone-change equivalent) already reuses a live (not consumed, not expired) challenge instead of creating a new one. **Decision (D5)**: if that live challenge's `SmsDeliveryJob` has reached terminal `failed`, calling `request_otp` again for the same phone does **not** create a new job — the existing challenge (and its dead job) simply lives out its now-5-minute TTL, after which a genuinely new challenge and job get created on the next request. There is no way to force a retry before that. This tradeoff was chosen explicitly over an auto-retry-on-resend design, to avoid interacting with the per-phone/IP rate limits (`_rate_limit()`) in ways that would need separate design.

## SMS Aero Client Design

- **Auth**: HTTPS Basic auth with the URL-encoded account email as username and API key as password. Credentials are sent in the authorization header, never embedded in a logged URL.
- **Endpoint**: `POST /v2/sms/send` with JSON fields `number`, `text`, and `sign`. Success = 2xx **and** parsed JSON has `success=true` **and** `data.id`; HTTP 200 with `success=false` remains an application failure.
- **Phone format**: normalize stored E.164 to a 7–15 digit integer without `+`, matching SMS Aero's official client and examples.
- **Limits**: message length is 2–640 characters; sender name is 2–64 characters. The runtime admin form enforces these limits and requires `{code}`.
- **Timeout handling**: an httpx timeout is a terminal ambiguous failure with no automatic retry, because SMS Aero may have accepted a billed message before the client timed out.
- **Retry policy (D3)**: definite non-timeout failures receive at most three claimed attempts; stale `sending` jobs fail closed because their external outcome is ambiguous.
- **Circuit breaker (D13)**: same pattern as `_TwoGisCircuitBreaker` — opens after 3 consecutive failures, process-wide module-level state. **Cooldown is shortened relative to 2GIS's 60s** (D19, red-team finding) — recommended 15–20s — because the combination of a process-wide breaker and the no-forced-resend UX (D5) means an open breaker blocks *all* users' OTP delivery, not just the caller whose request tripped it; a long cooldown eats a disproportionate share of the 5-minute challenge TTL for unrelated users.
- **Provider factory / stub mode (D7, D28)**: `sms_provider: Literal["stub", "smsaero"]` defaults to `"stub"`; enabling `"smsaero"` requires `SMSAERO_EMAIL` and `SMSAERO_API_KEY`. In stub mode jobs are marked `sent` without an HTTP call.
- **Emergency kill switch (D29, red-team finding)**: unlike the other `Literal["stub", ...]` provider settings (which require a deploy to change), `sms_provider` is additionally exposed as a runtime-editable flag via the existing `modules/runtime_config` module, so ops can disable real SMS sending mid-incident without a deploy — the static `Settings` value is the default; a `runtime_config` override, if present, takes precedence. (Implementation should confirm `runtime_config`'s exact override-precedence mechanics against its existing usages before relying on this.)

## SMS Content & Sender ID

- Message template and SMS Aero sender ID are editable at runtime via the admin panel (D12), not hardcoded or env-only, so ops/marketing can change wording without a deploy.
- Defaults: message `"Код подтверждения: {code}"`, sender name `"КРЫМТРИП"`.
- **Validation on save (D24, red-team finding)**: the admin save path rejects a template without `{code}`, unknown placeholders, text outside 2–640 characters, and sender names outside 2–64 characters. Sender-ID approval by SMS Aero remains the saving admin's responsibility.
- Implementation mechanism: prefer the existing `modules/runtime_config` module if it supports string-valued, admin-editable settings (unconfirmed at spec time — verify during implementation); otherwise a small dedicated settings row is acceptable.

## Cost & Rate Control

- **Existing rate limits are unchanged and considered sufficient** at the request layer: OTP request capped at 8/10min per IP and per phone independently (`_rate_limit()`), OTP verify capped at 20/10min. No new endpoint-level throttling is added.
- **Soft daily SMS budget (D14, refined by D22)**: the SMS send counter is stored in **Redis with a TTL to the next UTC midnight**, so it survives restarts/deploys. Exceeding a configurable daily threshold logs a warning only — it does not block sending.

## Admin Panel

- New `SmsDeliveryJobAdmin` SQLAdmin view (separate from `OtpChallengeAdmin`/`PhoneChangeChallengeAdmin`, following this codebase's one-view-per-model convention), showing: phone, status (badge-formatted per the existing `content_reports.status` convention), attempts, `provider_sms_id`, `last_error`, created/updated timestamps, and links back to the linked challenge.
- **View-only** (D11) — no manual "retry now" action, unlike `HelpIndexJob`'s existing manual-trigger precedent. Rationale: the 5-minute challenge TTL means a human almost never acts fast enough for a manual retry to matter before the code is dead anyway.
- `plaintext_code` is omitted from the admin list but available in the authenticated detail view while the job is pending; it is nulled at terminal status (see Security).

## Security Considerations

- **Plaintext OTP code at rest (D8)**: `SmsDeliveryJob.plaintext_code` is a new class of sensitive data this codebase hasn't stored before — `AuthOtpChallenge`/`AuthPhoneChangeChallenge` only ever store `code_digest` (SHA-256), with plaintext only ever surfacing via the local/test-only `debug_code` escape hatch. Storing it here is required because the poller needs the plaintext to retry a send. **Mitigation (D9)**: the column is set to `NULL` as soon as the job reaches a terminal status (`sent` or `failed`), bounding exposure to the job's brief in-flight window (well under the 5-minute challenge TTL in practice).
- **Debug shortcuts unchanged (D7)**: `AUTH_OTP_ACCEPT_ANY` and `AUTH_OTP_STORE_DEBUG_CODE` remain exactly as they are today, still refused outside local/test by `validate_settings()`. They are not retired now that a real provider exists.
- **Credential handling**: `SMSAERO_API_KEY` is `SecretStr | None`; it is never logged or returned. `validate_settings()` requires it and `SMSAERO_EMAIL` when the provider is enabled and requires an HTTPS gateway URL.
- **No new public endpoints**: because D6/no-webhook was chosen, this feature adds zero new attack surface reachable from the internet beyond the existing `request_otp`/`verify_otp` endpoints (already rate-limited).
- **Admin template safety**: validation accepts exactly one plain `{code}` placeholder and rejects attribute access, conversions, format specifications, duplicate fields, unknown fields, and provider-limit violations.

## Failure Modes & Operational Concerns

- **Alerting (D23, red-team finding)**: the spec previously had zero alerting, which combined with straight-to-production rollout (D15) and no delivery webhook (D6) meant a misconfigured API key or unapproved sender ID could silently break 100% of phone auth until a human happened to check `/admin`. **Decision**: add a log-level warning when the ratio of `failed` jobs over a recent time window (e.g. last 15 minutes) exceeds a threshold (e.g. >50%) — matching the existing 2GIS soft-budget log-warning idiom, no new alerting infrastructure (push/email/pager) required.
- **Rollout (D15)**: code ships with the safe static default `sms_provider=stub`; production activation requires setting the credentials and `SMS_PROVIDER=smsaero` (or using the validated admin switch after credentials are deployed). No real SMS is sent by tests.

## Testing Strategy

- SMS Aero HTTP client: tested via `httpx.MockTransport` injected into a real `httpx.AsyncClient`, including `success=false` inside HTTP 200, missing `data.id`, timeout-as-final-failure, Basic auth/body shape, phone normalization, and circuit-breaker transitions.
- Job/poller storage and claiming logic: hand-written in-memory fakes (`_RecordingSession`/`_CountingRedis`-style), matching `test_otp_challenge_storage.py` — including atomic claiming, retries, ambiguous failures, locking and terminal plaintext cleanup. Challenge/job co-creation is also covered by storage regression tests.
- **Coverage gate (D30, refined from D16)**: SMS Aero response parsing, circuit breaker, persistence and retry logic stay inside the normal coverage gate.

## Decisions Log

| ID | Topic | Decision | Rationale | Source | Date |
|---|---|---|---|---|---|
| D1 | Async dispatch mechanism | Persisted job table (`SmsDeliveryJob`), not in-process `asyncio.Task` or `BackgroundTasks` | No job infra exists in the repo at all; "джоба" implies a persisted record with admin visibility and restart-survival | Interview | 2026-09-16 |
| D2 | Job processing model | Immediate `asyncio.create_task` after commit, plus a background poller as safety net | Fast happy path, poller covers crash/failure recovery | Interview | 2026-09-16 |
| D3 | Retry policy | Fixed attempts + backoff for definite SMS Aero failures | Simple bounded v1 policy; ambiguous outcomes fail closed | Interview + provider amendment | 2026-09-16 |
| D4 | OTP TTL | Reduced from 10 to 5 minutes, for both login/registration and phone-change challenges | Bounds the "wait for natural expiry" resend flow (D5) | Interview | 2026-09-16 |
| D5 | Resend-on-failure UX | No auto-retry job on resend if the live challenge's job already failed terminally; user waits out the (now 5-minute) TTL | Avoids interaction with existing per-phone/IP rate limits | Interview | 2026-09-16 |
| D6 | "sent" status semantics | `sent` = SMS Aero accepted the request (`success=true` + `data.id`), not confirmed handset delivery; no callback | Avoids new public callback attack surface | Provider amendment | 2026-09-16 |
| D7 | Debug shortcuts & provider switch | Keep debug shortcuts unchanged; add `sms_provider: Literal["stub","smsaero"]` | Existing shortcuts remain useful for local/test | Provider amendment | 2026-09-16 |
| D8 | Plaintext code storage | `SmsDeliveryJob` stores the plaintext OTP code for the job's lifetime, unlike challenge tables (digest-only) | Poller needs plaintext to retry sending | Interview | 2026-09-16 |
| D9 | Plaintext code mitigation | Null the plaintext column once the job reaches a terminal status; omit it from the admin list while retaining authenticated detail visibility during investigation | Bounds both the exposure window and accidental shoulder-surfing while preserving ops diagnostics | Interview + implementation hardening | 2026-09-16 |
| D10 | Job-to-challenge linkage | Two independently-nullable FKs on `SmsDeliveryJob`, one per challenge type | Preferred over polymorphic ref (no FK constraint) or fully decoupled design (loses admin navigability) | Interview | 2026-09-16 |
| D11 | Manual retry in admin | View-only admin columns, no "retry now" action | 5-minute TTL makes manual retry rarely actionable in time | Interview | 2026-09-16 |
| D12 | SMS content/sender ID | Runtime-editable via admin, default message "Код подтверждения: {code}", default sender "КРЫМТРИП" | Ops/marketing need to change wording without a deploy | Interview | 2026-09-16 |
| D13 | SMS Aero client resilience | Process-wide circuit breaker opens after 3 consecutive failures for 20 seconds | Protects the provider during registration spikes | Provider amendment | 2026-09-16 |
| D14 | SMS cost budget | Soft daily budget counter, log-warning only, matching 2GIS idiom | Defense against spend spikes from abuse/registration surges | Interview | 2026-09-16 |
| D15 | Rollout | Deploy the code with `stub` as the safe default; activate SMS Aero explicitly after credentials and sender approval are installed | Prevents an incomplete secret/sender setup from breaking the first real production phone-auth flow | Implementation safety amendment | 2026-09-16 |
| D16 | Testing strategy | Reuse `httpx.MockTransport` + in-memory fake idioms from existing tests | Pattern-matched from repo conventions, not explicitly asked | Interview | 2026-09-16 |
| D17 | Poller concrete design | Fixed interval (~20s) plus a Redis single-flight lock to prevent overlapping sweeps | D2's cited "like HelpIndexJob" precedent doesn't actually exist (HelpIndexJob is manual, one-shot) — needed explicit design | Red Team | 2026-09-16 |
| D18 | Ambiguous send timeout | Terminal failure, no automatic retry | SMS Aero may already have queued the billed message; blind retry risks a duplicate | Red Team + provider amendment | 2026-09-16 |
| D19 | SMS circuit breaker cooldown | Shortened relative to 2GIS's 60s (recommended 15–20s) | Process-wide breaker + no-forced-resend (D5) means an open breaker blocks all users, not just the failing caller | Red Team | 2026-09-16 |
| D20 | SMS Aero success detection | Require 2xx, `success=true`, and `data.id` | API failures can still be represented in a JSON response to an HTTP request | Provider amendment | 2026-09-16 |
| D21 | FK invariant enforcement | DB `CheckConstraint` requiring exactly one of the two challenge FKs to be set | Matches existing codebase convention (`content_reports`); prevents orphaned/ambiguous rows from application bugs | Red Team | 2026-09-16 |
| D22 | SMS budget storage | Redis counter with TTL to UTC midnight | Counter survives restarts and frequent deploys | Red Team | 2026-09-16 |
| D23 | Alerting | Log-warning when failed-job ratio over a recent window exceeds a threshold | No monitoring existed; combined with straight-to-prod rollout (D15) and no webhook (D6), a misconfiguration would otherwise go unnoticed until a support ticket | Red Team | 2026-09-16 |
| D24 | Admin template validation | Reject template saves missing `{code}` or exceeding length limits | One bad admin edit would otherwise break 100% of sends with no safeguard | Red Team | 2026-09-16 |
| D25 | Challenge/job atomicity | Insert the challenge and delivery job in one database transaction; the poller recovers the durable pending job if dispatch never starts | Eliminates the crash window instead of reconstructing plaintext OTPs that are intentionally absent from challenge rows | Red Team resolution | 2026-09-16 |
| D26 | Double-send prevention | Atomic `UPDATE ... WHERE status='pending'` claim is the single concurrency-control point between immediate-trigger and poller | D2 didn't specify how the two dispatch paths avoid racing on the same job | Red Team | 2026-09-16 |
| D27 | Phone number format | Normalize E.164 to SMS Aero's 7–15 digit integer without `+` | Matches official examples and client validation | Provider amendment | 2026-09-16 |
| D28 | `sms_provider` default pattern | Static default `"stub"` everywhere, matching `routing_provider`/`tsp_provider`, not the env-conditional pattern | Removes ambiguity about fail-open vs fail-closed behavior if the env var is forgotten at deploy | Red Team | 2026-09-16 |
| D29 | Emergency kill switch | `sms_provider` also exposed as a `runtime_config`-overridable flag, not just a static `Settings` field | The highest-leverage incident-response lever shouldn't be the one thing requiring a deploy, given D12 already wanted no-deploy control elsewhere | Red Team | 2026-09-16 |
| D30 | Coverage gate scope | Keep SMS Aero transport, parser, breaker and delivery state machine in coverage | OTP delivery gates a security-critical auth flow | Red Team | 2026-09-16 |

## Dependency Graph & Implementation Order

```
1. Alembic migration 0059: SmsDeliveryJob table (D1, D10, D21) + AuthOtpChallenge/AuthPhoneChangeChallenge TTL constant change (D4)
   └─ depends on: nothing (schema-only change)

2. Settings: SMSAERO_EMAIL + SMSAERO_API_KEY (SecretStr), sms_provider Literal["stub","smsaero"], fail-fast validation
   └─ depends on: nothing

3. SMS Aero HTTP client (identity/infrastructure/sms_provider.py): HTTP Basic auth, v2 JSON parsing (D20), phone normalization (D27),
   circuit breaker (D13, D19), stub implementation
   └─ depends on: (2) settings

4. Provider factory (stub/smsaero switch) (D7, D28, D29 runtime_config override)
   └─ depends on: (3) SMS Aero client

5. SmsDeliveryJob creation + immediate-trigger dispatch, wired into _upsert_auth_otp_challenge /
   _upsert_phone_change_challenge (D1, D2, D8, D26 atomic claim)
   └─ depends on: (1) migration, (4) provider factory

6. Background poller (interval + Redis single-flight lock, retry scheduling, durable pending-job recovery,
   timeout-as-final-failure handling) (D17, D18, D25, D26)
   └─ depends on: (5) job creation

7. Soft daily SMS budget in Redis (D14, D22) + failed-ratio alerting (D23)
   └─ depends on: (3) SMS Aero client (needs call outcomes to count)

8. SmsDeliveryJobAdmin SQLAdmin view — view-only, status badges (D11, D9 plaintext-null-on-terminal visible in schema)
   └─ depends on: (1) migration

9. Admin-editable SMS template/sender ID + validation (D12, D24) — confirm runtime_config capabilities first
   └─ depends on: (3) SMS Aero client (needs to consume the template)

10. Tests: SMS Aero client (MockTransport), job/poller storage, coverage-scope decision (D16, D30)
    └─ depends on: (3), (5), (6) — written alongside each, not deferred to the end

11. Prod rollout: set SMS_PROVIDER=smsaero plus credentials after deploy (D15)
    └─ depends on: everything above being deployed and validated in code review (no staging gate per D15)
```

## Implementation Checklist

- [x] **1. Migration** — `0059_sms_delivery_jobs.py`, FK/status invariants, query indexes; OTP TTL reduced to 5 minutes
- [x] **2. Settings** — `SMSAERO_EMAIL`, secret API key, HTTPS base URL and `Literal["stub","smsaero"]` fail-fast validation
- [x] **3. SMS Aero client** — Basic auth header, `POST /v2/sms/send`, strict JSON success parsing, phone normalization, circuit breaker and ambiguous-timeout handling
- [x] **4. Provider factory** — safe stub default plus runtime emergency override
- [x] **5. Job creation + immediate dispatch** — challenge and job are committed atomically; immediate and poller paths share an atomic claim
- [x] **6. Background poller** — lifespan task, Redis owner-token lock, due-job sweep and fail-closed stale-claim recovery. A separate orphan sweep is unnecessary because challenge and job now share one transaction.
- [x] **7. Soft daily SMS budget** — Redis counter with TTL to UTC midnight and one threshold warning
- [x] **8. Alerting** — warning when the recent failed-job ratio exceeds 50% with a minimum sample
- [x] **9. Admin: `SmsDeliveryJobAdmin`** — view-only journal with status badge and terminal plaintext cleanup
- [x] **10. Admin: SMS template/sender config** — runtime-editable, validated `{code}`, provider limits and credential-aware activation
- [x] **11. Tests** — MockTransport contract/circuit/timeout tests plus OTP job/config/runtime regressions; no live paid SMS
- [ ] **12. Prod activation** — deploy credentials, approve `КРЫМТРИП` (or another sender) in SMS Aero, run migration, then set `SMS_PROVIDER=smsaero` and watch delivery failures

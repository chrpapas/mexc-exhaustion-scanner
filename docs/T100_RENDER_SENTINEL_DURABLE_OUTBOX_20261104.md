# T100/225 — Render sentinel persistent Discord alert outbox (2026-11-04)

**Status: reviewed engineering candidate, NOT deployed.**
The production target is two separate Render background workers using
managed PostgreSQL, not the Mac. The existing
`mexc-t100-live-shadow` worker and its `NATIVE_PROTECTION_LOST` kill
latch remain unchanged.

## Why this was needed

The first proposed Render sentinel
(`app/t100_render_shadow_sentinel.py`) kept deduplication only in process
memory. If it crashed just before/after a Discord send, pending alerts and
attempt history could vanish; restart could also re-send the same alert
indefinitely. The new independent entrypoint
`python -m app.t100_render_shadow_sentinel_durable` replaces that
candidate **only when explicitly configured and deployed**.

## Atomic persistence guarantee and remaining ambiguity

1. `migrations/032_t100_render_shadow_sentinel_outbox.sql` creates
   `public.t100_shadow_sentinel_state`, an append-only-by-policy
   `public.t100_shadow_sentinel_outbox`, and a PostgreSQL AFTER
   INSERT/UPDATE trigger. This is **CREATE-only**, non-idempotent
   migration; it never runs from a worker. Inspect and back up the
   existing database before applying it, and abort on existing objects.
2. When a new alert status is detected (or an alarm recurs after a
   healthy status), the state UPSERT and new pending outbox row are in
   the same PostgreSQL transaction. Identical repeated observations
   do not generate duplicate event rows.
3. The sentinel holds a session-scoped advisory singleton lock,
   `(110032,100225)`, so a second independently started instance
   refuses the same outbox scope. This is separate from the shadow
   writer's lock `(110031,100225)`.
4. Each pending alarm is read in insertion order, its send attempt is
   durably counted, then a redacted Discord-only message is sent.
   **Only successful sender acknowledgement** permits the row's
   `delivered_at` to be committed. A failed webhook/timeout leaves
   the row pending and retries on a later polling cycle or restart.
5. A crash **after Discord accepts the message but before the database
   acknowledgement** may send a duplicate. Therefore guarantees are
   **at-least-once**, not exactly-once. A down PostgreSQL instance cannot
   durably store alerts or independently alert; the worker fails closed
   and must be supervised by an external monitor. No claim of
   uninterrupted notification delivery is made.
6. A healthy witness status is nonauthorizing and does not clear the
   historical shadow worker's sticky protection-loss latch. No SQL
   table reset or outbox deletion is authorized by this tool.

## Separation of duties

Migration 031: `public.t100_shadow_witness` and durable
one-way shadow safety kill latch.

Migration 032: `public.t100_shadow_sentinel_state`,
`public.t100_shadow_sentinel_outbox`, and trigger.

A **separate sentinel database role** now requires:
- `USAGE` on `public`;
- `SELECT` ONLY on `public.t100_shadow_witness`;
- `SELECT, INSERT, UPDATE` ONLY on
  `public.t100_shadow_sentinel_state` and
  `public.t100_shadow_sentinel_outbox`;
- `USAGE` on outbox identity sequence;
- **NO** privileges on the live order tables, `DELETE`, `TRUNCATE`,
  DDL, or exchange credentials.

This is intentionally **not SELECT-only overall**: durable alert
acknowledgement requires narrowly scoped, auditable writes, entirely
separate from trading. Do not use the main `DATABASE_URL` owner or
the order executor's database credentials.

The shadow witness writer role remains limited to
`SELECT, INSERT, UPDATE` on `public.t100_shadow_witness` only.
Provision and review the two roles as a separate security operation,
not as an automatic deployment side-effect.

## Explicit service configuration

Keep the development-branch Render example
`docs/t100-render-shadow-witness-blueprint.example.yaml` as a
**manual-only example** (not the root `render.yaml`). For the second
worker, after schema/roles are reviewed:

- `startCommand: python -m app.t100_render_shadow_sentinel_durable`
- `T100_EXECUTION_MODE=paper`
- `T100_LIVE_ORDER_API_ENABLED=false`
- `T100_LIVE_KILL_SWITCH=true`
- `T100_RENDER_SENTINEL_ENABLED=true`
- `T100_RENDER_SENTINEL_DURABLE_OUTBOX_ENABLED=true`
- `T100_RENDER_SHADOW_SCOPE_SHA256`: operator-reviewed same Render
  account/scope binding as the shadow witness writer, NOT Mac pilot.
- `T100_RENDER_SENTINEL_DATABASE_URL`: sentinel-only restricted role.
- `T100_RENDER_SENTINEL_DISCORD_WEBHOOK_URL`: independent webhook
  secret configured privately in Render.
- `T100_RENDER_SENTINEL_STALE_SECONDS=240`
- `T100_RENDER_SENTINEL_POLL_SECONDS=45`
- **No MEXC API keys** and no automated live-entry/order settings.

## Test evidence and release criteria

- `tests/test_t100_render_shadow_sentinel_outbox.py` mocks the
  transactional trigger, restarts, failed webhooks and duplicate
  concurrent sentinel instances without exchange credentials.
- `tests/test_t100_render_shadow_postgres_integration.py` runs
  migrations 031 and 032 on a disposable localhost PostgreSQL 17
  container in GitHub Actions, including actual trigger, role and
  outbox lifecycle testing. It never accepts a production DB URL.
- This tests code/DDL; it does not prove that Render Postgres roles
  are configured, webhook messages appear in Discord, or the entire
  Render worker survives a real deployment/restart.
- Deployment of either new worker, actual schema migration, modifying
  existing Render worker, exchange credential permissions and order
  POSTs remain **not authorized by this engineering test**.
- The existing Render shadow worker's observed
  `NATIVE_PROTECTION_LOST` condition must be investigated rather
  than cleared through a fresh deployment.

**Formal gates remain blocked:** G6 monitored protection continuity,
emergency close and independent infrastructure; G5 stop amendment;
G9 native-stop execution and four-slot CROSS certification. No
production live trading authorization.

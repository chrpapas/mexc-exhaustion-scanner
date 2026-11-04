# T100/225 — durable Render shadow witness and independent Render sentinel

**Development checkpoint: November 4, 2026. NOT deployed. No live orders.**
The target production runtime is **Render background workers** in Frankfurt,
not the operator's Mac. This is a narrowly scoped observation-only
production-readiness increment. It is NOT a replacement for a certified
four-slot T100/225 executor.

## Verified existing Render infrastructure

Connected Render workspace: Trading bots.
- Existing worker `mexc-t100-live-shadow`, ID
  `srv-dbf5798m7kps73fqcuog`, Frankfurt, one instance,
  deployed branch `feature/t100-live-readiness-gates-20261023`,
  start command `python -m app.t100_live_shadow_runtime`, auto-deploy OFF.
- Existing Render Postgres `mexc-exhaustion-db`, ID
  `dpg-d9kb9djm8hqs73c3c39g-a`, Frankfurt.
- The live-shadow worker is currently reporting
  `OBSERVING_KILL_LATCHED`, `position_observed=True`,
  `tpsl_observed=False`, `kill_reason=NATIVE_PROTECTION_LOST`,
  `entry_allowed=false`, `live_authorization=false`.
  That is an existing **safety halt**, not a permission to resume or
  reset. The account is not the dedicated flat $50 Mac pilot.
- Direct read-only database inspection through the Render connector
  failed with SSL/TLS/remote-IP-allowlist errors. The database was **not
  modified**. Do NOT weaken its network allowlist just to enable that
  connector; server-side data can be checked later via a vetted internal
  runtime/role.

Render defaults to **ephemeral** service files. A local SQLite journal
in a different process/worker would not survive redeploy and is not
readable across services; therefore the proposed heartbeat uses **managed
Postgres** as its durable exchange-independent witness. Render's own
background worker restart is not the same as an independently supervised
risk emergency-close capability.

## Code implemented (development branch only)

- `migrations/031_t100_render_shadow_witness.sql` — explicit
  operator-reviewed, **non-idempotent** CREATE-only table and trigger.
  Immutable historical kill flag/reason, monotonic revision and
  non-authorizing `entry_allowed=false`, `live_authorization=false`
  columns. Does not run on service startup and is deliberately outside
  existing pinned live-order schema migrations.
- `app/t100_render_shadow_witness.py` — strict validated Postgres
  persistence adapter. Reader returns redacted status/revision/age.
  Writer records the actual `T100LiveShadowRuntime.cycle()` result via
  atomic UPSERT and cannot clear a prior kill latch. A **session-scoped
  PostgreSQL advisory singleton lock** refuses a simultaneous second
  witness writer before any MEXC GET.
- `app/t100_render_shadow_witness_runtime.py` — opt-in wrapper around
  the existing GET/REST-WS/Discord shadow cycle. After acquiring its
  Postgres singleton lock, it restores a historical kill latch **before**
  the first exchange observation. Each completed cycle counts only
  after committed Postgres witness acknowledgment. Missing database,
  malformed old row, concurrent runner, or commit failure causes
  nonzero exit with no live authorization.
- `app/t100_render_shadow_sentinel.py` — a **separate background
  process** with a SELECT-only database user, no MEXC API key or
  exchange client. It flags missing, malformed, stale or killed
  witness state and sends only redacted status codes through a
  separately provisioned Discord webhook.
- `tests/test_t100_render_shadow_witness.py` — pure, offline
  behavioral tests: persistent kill across restart, reject wrong
  reason/scope, database failure, duplicate writer singleton lock,
  stale/missing/read error, no MEXC secrets in sentinel, redacted
  alert contract. The dedicated CI workflow executes these checks
  without live exchange traffic.
- `docs/t100-render-shadow-witness-blueprint.example.yaml` — an
  **illustrative, non-root** Render worker blueprint with
  `autoDeployTrigger: off` and no secrets. This is not synced or
  running and must not be used to alter the existing worker until
  release review.

## DB roles and separate services: approval required

Before any deployment or migration:
1. Take and verify a backup of the existing Render Postgres. Review
   `031` as separate schema change. It must never be applied
   automatically by a worker, and any existing objects require manual
   review (non-idempotent DDL will refuse them).
2. Provision a narrowly scoped **witness writer** database role
   permitted `SELECT, INSERT, UPDATE` only on
   `public.t100_shadow_witness`. Do not grant DELETE, TRUNCATE,
   DDL, broad `public` table access, or live-order-table privileges.
   Verify that advisory session locking is available.
3. Provision a separate **sentinel reader** role allowed `SELECT`
   only on that one table, with NO other user table access or MEXC
   credentials. Inject distinct database URLs privately in Render.
4. Precommit a `T100_RENDER_SHADOW_SCOPE_SHA256` locally for the
   actual Render shadow account. **Do not borrow the Mac pilot's
   account/key fingerprint.** A hash string is a scope binding, not
   proof of server-side MEXC account identity.
5. Provision a separate sentinel Discord webhook. Avoid putting
   credential values in code, GitHub commits, chat, CI logs or docs.
6. Deploy two **new, disabled-by-default review candidates** or perform
   an explicitly reviewed guarded cutover of the existing read-only
   worker. Do not start duplicate GET shadow sessions against the same
   account without reviewing their roles, ownership and rate limits.
   Set `T100_EXECUTION_MODE=paper`,
   `T100_LIVE_ORDER_API_ENABLED=false`,
   `T100_LIVE_KILL_SWITCH=true` for both.
7. Run a real Render soak, verify independently observed Discord
   delivery, stop/restart persistence, and observer-stale alerting
   during a supervised Render failure test. Record actual commit,
   service IDs, deploy IDs, DB witness revision and operator review.

## Open blockers (not implied complete)

- The existing Render shadow worker still reports a protection-loss
  kill latch and an observed position with no current native stop.
  Do not reset or redeploy it to erase that evidence.
- PostgreSQL migration 031 and least-privilege roles NOT applied.
  The direct connector database query was denied by network/SSL policy,
  so actual schema absence/presence was not verified.
- No new Render worker has been created, deployed or tested.
- The sentinel as implemented has **in-memory notification cooldown**,
  not a durable delivered-message outbox. On process restart,
  at-least-once notification can duplicate; repeated failures can
  delay retry. This needs strengthening before production certification.
- No independent-host/channel redundancy, verified emergency-close
  runner, actual native stop amendment/trigger/partial-fill drill,
  funded CROSS/four-slot envelope, or independent launch approval.
- Existing witness schema does not itself certify venue account
  ownership, exchange order permission or crash/POST uncertainty.
  All code paths explicitly remain **nonauthorizing**.

**G6 remains NOT CERTIFIED. G5/G9 remain BLOCKED.**
This work does not change the existing running service, credentials,
Render variables, database schema, BOXWAVE, or MEXC orders.

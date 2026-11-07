# Durable Render shadow deployment preflight — NOT approved for execution

## Verified inventory

Workspace: Trading bots (`tea-d3oimrjipnbc7381g6jg`), confirmed by operator.
Existing shadow: `srv-dbf5798m7kps73fqcuog`, Frankfurt, single worker,
autoDeploy=no/off, branch feature/t100-live-readiness-gates-20261023.
Live deploy: `dep-dbl2tk61egvs739d9qqg`, commit
`a9692afbd0cedd314054427e37ce1aac70f75577`, live since 2026-11-03.
Database: `dpg-d9kb9djm8hqs73c3c39g-a`, mexc-exhaustion-db,
logical database mexc_exhaustion, PostgreSQL 17, available, Frankfurt,
15 GB disk, plan 1c-2g, no HA. External allowlist remains two /32 entries.
No new candidate services appear in the confirmed workspace inventory.

Logs at 2026-11-04T20:36:37.491008367Z, 20:38:07.78480295Z and
20:39:37.94233992Z show run d4baebb3c115, cycles 1025–1027,
OBSERVING_KILL_LATCHED, position_observed=True, tpsl_observed=False,
NATIVE_PROTECTION_LOST, entry_allowed=false and live_authorization=false.
These are corroborating recent observations, not the original incident time.
Existing historical evidence must remain retained. Do not seed flat/healthy.

## Access and backup blockers

Read-only connector query failed before returning rows (EOF/TLS-required).
No installed migration/object state or SQL-session TLS was verified. Hosted
connector limitations do not authorize broader external network access.
Render tools expose no backup listing. Browser fallback could not open Chrome:
Computer Use permissions are not granted. No Render CLI/psql found on PATH.
Backup timestamp, retention, recoverability and last successful export remain
UNVERIFIED. Paid-plan PITR support is not evidence of this database's backup.

## Exact migration candidates (apply neither yet)

Only these full paths, not every migration with numeric prefix 031/032:

- migrations/031_t100_render_shadow_witness.sql
  SHA256 03252d80d687cbf70e89d0a2d316d49f8f433fbba55b1d0a4d1d0305bb90645f
- migrations/032_t100_render_shadow_sentinel_outbox.sql
  SHA256 16c8ad56cfa9148fb8802fc8c1037a9be98a1c233539c8d60b17bd26290928e4

After backup/target verification, inventory all three tables, functions,
triggers, index and identity sequence plus role names. Existing objects require
comparison and a revised plan; never DROP/REPLACE or IF NOT EXISTS to bypass.
Apply reviewed files in one explicit transaction with lock_timeout=5s and
statement_timeout=60s, abort/rollback on any conflict. No automatic startup DDL.
Trading migrations with matching numeric prefixes are excluded.

## Roles and seed plan to finalize after SQL inspection

Separate new roles: t100_shadow_witness_writer and t100_shadow_sentinel_outbox.
Both NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS, no role
memberships, no ownership, no broad grants. Provision credentials privately.
Writer: CONNECT to target DB, USAGE public, SELECT/INSERT/UPDATE witness only.
Sentinel: CONNECT, USAGE public, SELECT witness; SELECT/INSERT/UPDATE only on
sentinel_state/outbox; USAGE only on the outbox identity sequence.
Verify effective permissions including PUBLIC, inherited roles and existing
schema/database grants: no CREATE, DELETE, TRUNCATE, trading-table SELECT or
mutation. If PUBLIC supplies access, STOP for an explicitly reviewed solution;
do not revoke shared privileges globally and risk existing production workers.
Perform negative privilege assertions in rollback-only transactions, with no
trading data read or mutation. Do not use the database owner in either worker.

Seed only after actual Render-account scope is verified separately from the
Mac pilot. Preserve original incident evidence and original reason. Review an
explicit row using observed run/cycle/time, kill_latched=true,
kill_reason=NATIVE_PROTECTION_LOST, state=OBSERVING_KILL_LATCHED and both
permission booleans false. Never set observed_at to migration time to make old
evidence appear fresh. Verify seed with both restricted roles before startup.

## Service and notification approval stages

Prepare two separate manual-deploy workers from the reviewed development SHA,
autoDeploy off, single instance, paper, order API false, kill switch true.
Use existing blueprint example as draft only. Writer must pin historical reason
NATIVE_PROTECTION_LOST and require the seeded row before any exchange GET.
Sentinel gets only its DB credential and independent Discord webhook, no MEXC
credentials or inherited secret environment group. Validate certificate-checked
TLS and distinct roles. Record exact plans/prices before paid-resource approval.

Do not start a second account observer alongside the old worker without an
approved ownership/rate-limit cutover plan. First deploy sentinel against the
historical seed, verify truthful killed/stale alerts and actual Discord receipt.
Then, only with explicit service-change approval, perform observer cutover while
preserving the old deployment and latch evidence for rollback. A background
worker has no Render HTTP health check: require committed witness revisions,
actual logs, outbox attempts/delivery and independently observed Discord receipt.
Test sentinel failure/restart and duplicate-at-least-once handling after separate
approval. Shared database outage still affects both; do not claim redundancy
against database/provider failure. Never erase witness/outbox rows for a drill.

## Validation

All 15 GitHub runs at requested checkpoint 887a4cf2 passed, including isolated
shadow PostgreSQL and role proofs. Local integrated tree equals that checkpoint
before TLS hardening. New TLS change forces certificate/hostname verification
and does not retry plaintext; live TLS connectivity remains to be demonstrated.
No DB mutation, resource creation, deployment, secret change, Discord send or
exchange POST occurred during this preflight.

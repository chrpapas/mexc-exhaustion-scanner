# T100 — 27 October 2026 DB commissioning checkpoint

**Status: DATABASE UNVERIFIED / LIVE MUTATIONS BLOCKED.**

Render workspace: Trading bots, Frankfurt. Existing database is
`mexc-exhaustion-db` (PostgreSQL 17), also used by the existing production
scanner/trader services. Shadow worker is a separate read-only service.

## What was checked

- The Render PostgreSQL instance is reported AVAILABLE.
- Connected Render's read-only SQL query failed during connection negotiation:
  external-IP allowlist and SSL/TLS restrictions. **No SQL ran.**
- Do **not** weaken the external allowlist to make the hosted connector work.
- The Render connector exposes only read-only SQL, so migrations cannot be
  applied through that connection even if connectivity is restored.
- Migrations 027, 028, 029, 030 and a runtime privileges template exist on the
  feature branch. Their applied status on the existing database is **unverified**.
- A live runtime must not use the existing table-owner/scanner credential for
  append-only accounting: ownership and application write permissions must
  be separated before permitting trading.

## First permitted operator operation: read-only inventory (no MEXC API)

Run from an already permitted, trusted Mac. Do not paste the database URL
into ChatGPT, PR comments, logs, or screenshots.

```bash
cd ~/trader-backtest/mexc-exhaustion-scanner-live-readiness-git
git pull --ff-only origin feature/t100-live-readiness-gates-20261023
source .venv-t100/bin/activate
T100_EXECUTION_MODE=paper python -m app.t100_live_db_inspect --prompt-dsn
```

The inspector prompts for the URL with hidden input, does not export the URL
into the shell environment, checks database identity against `mexc_exhaustion`,
opens one `ssl=require` PostgreSQL connection and wraps all queries in
`READ ONLY`. It does not use exchange credentials,
write SQL, submit orders, or change the Render deployment.

Possible expected statuses:
- `DATABASE_CONNECTION_FAILED`: TLS/network/credentials prevented connection.
- `WRONG_DATABASE_IDENTITY`: the URL did not point to `mexc_exhaustion`.
- `LIVE_DATABASE_MIGRATION_PREREQUISITES_MISSING`: some new tables are absent.
- `FULL_STRATEGY_STORAGE_SHAPE_UNVERIFIED`: VOL30/lease schema mismatch.
- `FULL_STRATEGY_STORAGE_PRIVILEGES_UNVERIFIED`: VOL30/lease runtime grants mismatch.
- `LIVE_SCHEMA_OR_RUNTIME_PRIVILEGES_NOT_VERIFIED`: ledger table/privilege mismatch.
- `LIVE_DURABLE_STATE_NOT_CLEAN`: unresolved intent/amendment/review state.
- `LIVE_DATABASE_READ_ONLY_PRECHECK_PASSED`: the queried role passed the
  schema/privilege and unresolved-state gates; **not** live authorization.

If this first check runs under the table-owner credential, a privileges gate
failure may be expected. Run the final gate again under a separately restricted
runtime role after a reviewed migration/permission rollout.

## Operator backup and atomic migration gate

1. Open `mexc-exhaustion-db` in the Render Dashboard. On **Recovery**,
   click **Create export** (Render's logical backup), wait for it to finish
   and record the export timestamp. Prefer downloading the export and
   verifying it is readable, and confirm the database has PITR available.
   A pending export is not a verified backup.
2. Before SQL writes, run the guarded read-only plan:

```bash
cd ~/trader-backtest/mexc-exhaustion-scanner-live-readiness-git
git pull --ff-only origin feature/t100-live-readiness-gates-20261023
source .venv-t100/bin/activate
T100_EXECUTION_MODE=paper python -m app.t100_live_db_migration --plan
```

   Supply the same Render external URL at the hidden prompt. Expected result:
   `MIGRATION_PLAN_READY_AFTER_BACKUP` and
   `SAFE_EMPTY_LIVE_NAMESPACE` with `live_tables_present=0`.
3. The separate `--apply --backup-export-verified` command is available
   only when an export has actually succeeded and been independently
   reviewed. It is an explicit operator action: it requires typing
   `APPLY 027-030 TO mexc_exhaustion`, pins all four SQL file contents to
   reviewed Git blob SHA-1 checksums, locks migrations with a Postgres
   advisory transaction lock, refuses partial/pre-existing live namespaces,
   and creates all twelve live tables atomically with post-DDL checks.
   It will *never* change the existing scanner/trader tables.
4. **Do not run apply without backup/recovery verification and independent
   migration review**, even if the read-only plan passes. The script does not
   create the runtime DB role. After DDL, configure the role with
   `sql/t100_live_runtime_privileges_template.sql` separately, then run
   `app.t100_live_db_inspect --prompt-dsn` with that restricted credential.

There is no safe one-command action that completes DDL, restricted role
creation, real Postgres fencing tests, and MEXC execution certification
simultaneously. Each gate must be checked before the next.

## Next phase — gated, not yet performed

1. Review a recoverable backup/restore point.
2. Confirm migration sequencing and identify a *separate* live runtime role.
3. Apply **027, 028, 029, 030 in order** only from a trusted, authorized
   connection after examining existing table definitions and confirming a
   recoverable backup. Keep orders disabled.
4. Verify exact table/column/index/sequence definitions, privileges, and
   clean durable state. Do not infer migration success from IF NOT EXISTS alone.
5. Exercise lease renewal/expiry/takeover against PostgreSQL with a test
   lease name before connecting any MEXC write adapter.
6. Only after DB checks: run real-account GET-only readiness and obtain
   independent code + emergency runbook review.

## Fencing issue corrected on 27 October

An expired leader lease reacquired with the same holder ID formerly retained
its fencing token. The SQL now increments the token whenever the previous
lease is expired, including same-holder reacquisition. Add a unique
process-lifetime holder ID for each future worker process. This is verified in
offline CI; the real PostgreSQL lease exercise is still outstanding.

**None of these steps changes `LIVE_ADAPTER_STATUS=NOT_IMPLEMENTED`,
`entry_allowed=false` or `live_authorization=false`.**

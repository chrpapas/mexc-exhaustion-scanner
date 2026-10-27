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
export T100_EXECUTION_MODE=paper
read -s -p "Render external PostgreSQL URL (hidden): " DATABASE_URL; echo
export DATABASE_URL
python -m app.t100_live_db_inspect
unset DATABASE_URL
```

The inspector opens one `ssl=require` PostgreSQL connection and wraps
all inspection queries in `READ ONLY`. It does not use exchange credentials,
write SQL, submit orders, or change the Render deployment.

Possible expected statuses:
- `DATABASE_CONNECTION_FAILED`: TLS/network/credentials prevented connection.
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

# 2026-10-28 T100 live PostgreSQL runtime-role commissioning

**State: Schema created; trading disabled; restricted runtime role NOT YET created.**

## Evidence from operator / Mac

- Render logical Recovery export: operator attests that it has completed
  and is accessible. Independent restore validation has not been performed.
- `mexc_exhaustion`, schema `public`: operator's guarded migration report
  returned `T100_LIVE_TABLES_APPLIED_ATOMICALLY`, 12 required tables and
  required columns/indexes/sequences verified, no MEXC requests/orders.
- Runner used a single Postgres DDL transaction (027, 028, 029, 030).
- **Live authorization is false.** The current shadow remains latched after
  MEXC `contract_specs` ReadTimeout; this is unrelated to the DB DDL.

## Why the next step is a separate role

The original Render database role `mexc_scanner` owns the production
scanner/paper tables and likely also owns the new live tables. Table owners
always bypass ordinary GRANT/REVOKE checks for table privileges. It must
**never** be used as the T100 autonomous live runtime credential.

Render-managed new PostgreSQL credentials become the **new default user**
and can alter which credentials Blueprint-managed database URL variables
will receive on a future Blueprint sync. Do not click `+ New default
credential` casually to solve runtime separation.

If `mexc_scanner` has PostgreSQL `CREATEROLE`, a **distinct SQL-created
login** (not managed by Render) can avoid default credential rotation.
If it lacks `CREATEROLE`, stop and review the Render-managed option
and downstream Blueprint effects before creating any new default user.

## Read-only prerequisite probe

From the trusted Mac with the already-working owner URL:

```bash
cd ~/trader-backtest/mexc-exhaustion-scanner-live-readiness-git
git pull --ff-only origin feature/t100-live-readiness-gates-20261023
source .venv-t100/bin/activate
T100_EXECUTION_MODE=paper python -m app.t100_live_role_preflight --prompt-dsn
```

Paste the existing owner/Render External URL at the hidden prompt. No URL
is printed, no role is created, no permission changes are performed.
The script verifies:
- correct database and explicit READ ONLY transaction;
- all 12 live table owners are the connected role;
- all 4 BIGSERIAL sequence owners are that same role;
- whether it has `CREATEROLE` or superuser status;
- whether the proposed `t100_live_executor_runtime` role already exists.

If `DIRECT_ROLE_CREATION_PREFLIGHT_PASSED`:
- review creation of a separate `LOGIN NOINHERIT NOSUPERUSER NOCREATEDB
  NOCREATEROLE NOREPLICATION NOBYPASSRLS` role with a strong secret;
- use owner privileges to apply
  `sql/t100_live_runtime_privileges_template.sql` substituting ONLY
  the vetted role identifier, and restrict database CONNECT/public schema
  privileges if needed;
- verify `app.t100_live_db_inspect --prompt-dsn` under the new dedicated
  **runtime** URL, not the existing scanner or owner URL;
- test that runtime is not table owner, cannot write to existing production
  scanner/paper tables, cannot UPDATE immutable evidence, and can acquire/
  release a test leader lease. Do not test actual trading.

If `OWNER_LACKS_CREATE_ROLE`: stop before trying to modify role privileges.
The fallback may need a Render-managed credential with explicit handling
of default-user changes; do not rotate the current app credentials
unintentionally.

This is **database commissioning only**. Full strategy replay parity,
MEXC read reliability, signed live execution, loss and liquidation exposure,
independent review, and operator authorization are separate launch gates.

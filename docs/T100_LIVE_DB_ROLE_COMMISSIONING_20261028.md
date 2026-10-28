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

If `DIRECT_ROLE_CREATION_PREFLIGHT_PASSED`, run the guarded
manual-only provisioning command (after independent review and operator
approval):

```bash
cd ~/trader-backtest/mexc-exhaustion-scanner-live-readiness-git
git pull --ff-only origin feature/t100-live-readiness-gates-20261023
source .venv-t100/bin/activate
T100_EXECUTION_MODE=paper python -m app.t100_live_role_setup --apply
```

- Type the exact in-terminal confirmation when requested.
- Use a fresh unique strong password of at least **32 printable ASCII
  characters**, stored in a password manager. It is entered and confirmed
  with hidden input, is never printed by the script, and is not put in
  the shell history or GitHub.
- Supply the **existing owner/Render External PostgreSQL URL** via the
  separate hidden prompt.
- The runner creates only the fixed `t100_live_executor_runtime` role
  with `LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  NOBYPASSRLS` and a connection limit, applies the fixed **hash-pinned**
  `sql/t100_live_runtime_privileges_template.sql` in a transaction, then
  connects as the *new* user with the existing host/database and verifies:
  12 new tables and restricted column-level permissions, 4 sequence grants,
  clean durable state, absent CREATE privileges and zero privileges on
  production/paper tables.
- A failed grant/role-attribute check rolls the DDL transaction back. A
  subsequent failed login/effective privilege check may leave the new role
  in place but must not authorize anything. **Do not retry or drop the
  role blindly**; inspect its state first.
- No Render managed default credential is rotated or removed, no production
  worker is restarted, and no MEXC API calls occur.
- The first write-path test after successful grants is a **test-only
  leader-fencing lease** against PostgreSQL. It must be performed before
  real executor deployment.

If `OWNER_LACKS_CREATE_ROLE`: stop before trying to modify role privileges.
The fallback may need a Render-managed credential with explicit handling
of default-user changes; do not rotate the current app credentials
unintentionally.

This is **database commissioning only**. Full strategy replay parity,
MEXC read reliability, signed live execution, loss and liquidation exposure,
independent review, and operator authorization are separate launch gates.

# V40 isolated T100 WEB pilot — after signal reader role creation

Operator's independent DB audit returned:
`V40_SOURCE_READER_SELECT_ONLY_COLUMNS_AUDITED_NONAUTHORIZING`,
`source_readonly_role_exists=true`,
`all_table_write_privileges_absent=true`,
`full_table_select_privileges_absent=true`,
`checked_tables=40`, `mexc_orders_sent=0`.

The scanner and separate-account shadow are not changed by the commands
below. The existing V40 trader is still set to launch the disabled
networkless holding runner, deliberately not a live order process.

## 1. Add one SECRET DSN to existing V40 trader, not scanner

Open Render worker `mexc-standard-short-trader`
`srv-d9v1r42d0e5s73eemsv0`, Environment. Keep existing
`MEXC_API_KEY` and `MEXC_API_SECRET` for the dedicated T100 WEB
subaccount unchanged. Do not reuse these with BOXWAVE.

Create `T100_V40_SIGNAL_READER_DSN` as a **secret environment value**
using the *internal* Render database hostname for `mexc-exhaustion-db`,
database `mexc_exhaustion`, exact role `t100_v40_signal_reader`
and the **new role password** generated when creating that role.
It must NOT use the owner `mexc_scanner` password/URL.
Example shape, not a live credential:

```text
postgresql://t100_v40_signal_reader:<URL-encoded-source-role-password>@<internal-db-hostname>:5432/mexc_exhaustion
```

Password characters reserved in URLs need percent encoding. Do not copy
actual credentials to GitHub, chat, shell arguments, logs or screenshots.

Keep current safety flags exactly as deployed:
`T100_V40_EXECUTOR_DISABLED_WORKER=true`,
`T100_V40_EXECUTOR_KEY_PREFLIGHT_ENABLED=true`,
`T100_V40_ACCOUNT_PROFILE=DEDICATED_WEB_PILOT`,
`T100_EXECUTION_MODE=paper`,
`T100_LIVE_ORDER_API_ENABLED=false`,
`T100_LIVE_KILL_SWITCH=true`,
`EXECUTION_ENABLED=false`,
`TRADING_MODE=paper`.

Do not restore the owner `DATABASE_URL` or old broad DB roles.
Save and deploy the new environment only AFTER verifying the service
start command stays
`python -m app.t100_v40_render_executor_disabled_worker --run`.

## 2. Run actual V40 source -> MEXC signed GET proof

In the same worker's Render Shell, run:

```bash
python -m app.t100_v40_render_signal_intake --once
```

This connects with the restricted read-only DB role, checks
`current_user=t100_v40_signal_reader`, one REPEATABLE READ /
READ ONLY transaction, all source Stage2 rows in the **last 900 seconds**
and exactly 49 causal Min30 bars for every candidate. Only if exactly
one candidate with VOL30 <30 exists does it authenticate to MEXC
using the isolated pilot key, require TWO consistent flat/hedge account
GETs, and compute the frozen V40 modeled short notional capped to
min($12.50, 25% of available/equity).

`V40_SIGNAL_INTAKE_FRESH_EMPTY_SOURCE_OBSERVED` means source access
works and there is *currently* no qualifying signal.
`V40_GENUINE_SIGNAL_AND_FLAT_ACCOUNT_MODELED_NONAUTHORIZING` means
there is one real checked candidate, still NOT a trade permit.
Any unknown role, stale/partial source, nonflat account, lost funds
or network failure HALTS. No order can be transmitted.

Once --once works, --watch is an optional long-running **GET-only**
debug loop every 60 seconds. Do NOT replace the worker start command
or label it the live trader until supervised review.

## 3. Plan the missing durable one-shot order-claim schema

The V40 immutable journal objects in migrations 031/032 are still
not confirmed as deployed. A separate new opt-in CLI checks database
schema/privileges without writing, using a **privileged owner URL
entered privately at prompt**:

```bash
python -m app.t100_v40_claim_journal_migration --plan
```

Expected before first deployment:
`V40_JOURNAL_CREATE_ONLY_PLAN_PASSED`.
The command detects missing base 027–030 objects and any partial
V40 objects and will refuse to apply on ambiguity.

ONLY after a verified database export/backup and independent review,
a separately approved operator can choose:

```bash
python -m app.t100_v40_claim_journal_migration --apply --backup-export-verified
```

An exact terminal confirmation is required. It creates only
V40 031/032 tables, indexes and immutable trigger in ONE transaction,
pins Git blob content, checks emptiness, uses an advisory lock and
never grants runtime writer privileges. It cannot send MEXC orders.
Never rerun --apply if a prior command is ambiguous: check schema
and reconcile the outcome.

## 4. Why real orders are still disabled

The release still requires a restricted **writer** DB role/grants for
the new journal objects; a fully composed V40 long-running executor;
verified CROSS 1x, native attached 75%-above-entry short stop and
continuous nonflat protection; stale/duplicate-safe durable POST claim;
signed-fill-based frozen trailing exits and independent recovery
so unknown POSTs never retry. In shipped guards,
`LIVE_ADAPTER_STATUS=NOT_IMPLEMENTED`, so no true live V40 permit.
A successfully authenticated subaccount and a new DB source role
alone cannot provide that permit.

The user has authorized a bounded isolated real-money pilot (about
56 USDT initially); losing the pilot capital willingly does not prove
native stop functioning or limit possible exchange losses.

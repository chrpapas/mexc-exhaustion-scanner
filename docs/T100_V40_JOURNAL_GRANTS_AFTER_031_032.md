# V40 after 031/032 journal migrations — restricted executor rights

Operator reported production migration result:
`V40_JOURNAL_031_032_CREATED_WITHOUT_RUNTIME_GRANTS`,
`tables_created=2`, `immutability_trigger_present=true`,
`runtime_role_granted=false`, `mexc_orders_sent=0`.

This creates the tables but **does not** authorize any live trading.
The source role `t100_v40_signal_reader` has already been independently
created and audited SELECT-only. Never grant it INSERT or UPDATE anywhere.

## A. Inspect executor role and both new journal tables

In Render Shell for the isolated V40 trader, on an updated code commit:

```bash
python -m app.t100_v40_journal_runtime_grants --plan
```

The script will securely prompt for the existing PostgreSQL *owner* URL
(the same owner account used for the 031/032 migration). It does only
read-only catalog/privilege queries, no MEXC API calls.

Possible outcomes:

- `V40_JOURNAL_RUNTIME_MINIMAL_GRANTS_PLAN_PASSED`: the *separate*
  `t100_live_executor_runtime` login exists with restricted attributes;
  V40 journal tables are empty, immutable claim trigger active, owner
  identity correct, there are no preexisting ambiguous V40 ACLs, and
  the independent source reader has no journal access.
- `V40_JOURNAL_RUNTIME_ROLE_ABSENT_USE_BASE_ROLE_SETUP`: **do not
  apply grants**; first use the separately reviewed base-runtime role
  provisioning and independent login audit. Its read-only plan is:

```bash
python -m app.t100_live_role_preflight --prompt-dsn
```

  `app.t100_live_role_setup` has a separate explicit operator-controlled
  role creation workflow. Do not repurpose `mexc_scanner`, source reader,
  BOXWAVE credentials or MEXC API credentials as the runtime database login.
- Any other status: inspect the actual role/schema/ACL issue; do not
  run an unreviewed GRANT or forcibly restart the trader.

## B. Only if the plan passed and backup is confirmed

The opt-in journal privilege command requires exact typed approval and
private owner URL and existing restricted *runtime* password:

```bash
python -m app.t100_v40_journal_runtime_grants --apply
```

It executes exactly these **three** grants, in one owner transaction:

```sql
GRANT SELECT, INSERT ON TABLE public.t100_live_v40_slot_reservations TO t100_live_executor_runtime;
GRANT UPDATE (state, updated_at) ON TABLE public.t100_live_v40_slot_reservations TO t100_live_executor_runtime;
GRANT SELECT, INSERT ON TABLE public.t100_live_v40_dispatch_claims TO t100_live_executor_runtime;
```

No DELETE, TRUNCATE, broad UPDATE, schema DDL, other-account data,
scanner rights or additional MEXC order rights. The source reader remains
unable to access either journal. The immutable trigger blocks ordinary
UPDATE/DELETE on dispatch-claim rows.

The command then **logs in on a separate connection** as the actual
restricted `t100_live_executor_runtime` to check effective SELECT/INSERT,
column-specific UPDATE, DDL denial, claim trigger enablement and absence
of forbidden rights. Successful terminal status:

`V40_JOURNAL_RUNTIME_MINIMAL_PRIVILEGES_VERIFIED_NONAUTHORIZING`

**If the GRANT stage committed but independent login audit failed, don't
rerun --apply.** Inspect and reconcile existing ACL/role first.

## C. Remaining hard blockers before the first true V40 MEXC order

The actual strategy must run at a genuine fresh Stage2/VOL30 candidate,
in the isolated T100 WEB account, subject to venue lot/minimum, equity,
CROSS 1x sizing, verified native SL75 MARKET, repeatable account/book
witnesses, durable one-shot order claim, independently running protection
sentinel, signed-fill trailing exit and crash recovery. Existing
`LIVE_ADAPTER_STATUS=NOT_IMPLEMENTED` remains in force.
No backtest or 'willing to lose 56 USDT' statement certifies those.
Do not flip the current disabled Render worker's execution flags or
restart an old trader.

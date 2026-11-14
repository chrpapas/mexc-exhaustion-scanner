# T100 V40 actual-money single-position pilot — live cutover state

Status: **NOT LIVE ORDER ENABLED**. The operator explicitly authorized a
small real-money test of the authentic frozen V40 strategy on the
isolated T100 WEB pilot subaccount (observed 56.37671495 USDT equity);
**no synthetic/random BTC trades**. This document records the actual
state, not a claim that user risk acceptance alone proves venue safety.

## What is genuinely operational

- Existing Render scanner `srv-d9kba2jm8hqs73c3dfi0` processes fresh
  Min30 scanner cycles. Recent Stage2 eligible counts have been zero.
- Isolated trader Render worker `srv-d9v1r42d0e5s73eemsv0` deploys the
  V40 branch with the existing start command
  `python -m app.t100_v40_render_executor_disabled_worker --run`.
- Explicit `T100_V40_REAL_SIGNAL_MONITOR_ENABLED=true` and
  `T100_V40_MONITOR_SINGLE_POSITION_SCOPE=true` activate ONLY a live-data
  signal/account monitor. No MEXC POST methods are imported into this worker.
- Separate source login `t100_v40_signal_reader` is restricted to
  scanner Stage2/Min30 columns. Trading executor login
  `t100_live_executor_runtime` already exists and has independently
  audited minimal 031/032 journal table ACLs.
- Migrations 031 and 032 were applied, immutable claim trigger present.
- `app.t100_v40_render_supervised_pilot_worker` reads fresh genuine Stage2
  candidates, VOL30 admission, bookended isolated MEXC account inventory.
  After a genuine candidate, it asks candidate-only public-contract and
  separately signed private SHORT CROSS 1x permission witnesses.
- The journal observer
  `python -m app.t100_v40_render_journal_readonly` uses only a *separate*
  restricted `T100_V40_EXECUTOR_RUNTIME_DSN` to check the new journal,
  unresolved legacy live orders and prior claims with REPEATABLE READ
  READ ONLY. This DSN must not be the scanner-owner or signal-reader DSN.
  It must be configured securely by the operator if missing; no password
  should be pasted into chat.

## Technical release blockers, all independent of risk consent

1. A real frozen V40 candidate has not appeared. Only a genuine fresh
   Stage2 signal passing all frozen filters can be used.
2. Exact lot-quantized intended notional, one-position CROSS 1x margin,
   current book depth and daily loss after fees require a live validated
   contract witness for that exact candidate; generic one-contract BTC
   capability probes do not prove this.
3. Existing one-shot order coordinator remains **not a service**.
   `LIVE_ADAPTER_STATUS=NOT_IMPLEMENTED` and the typed permit prevent MEXC
   POST. Do **not** patch this constant or forge evidence booleans to force
   activation.
4. A reliable **native whole-position MARKET SL75** attachment and
   signed-GET protective identity must work for the actual V40 position.
   A successful `order/create` ACK alone does not guarantee protection.
5. Post-entry exact fills, checkpoint persistence and restart reconciliation
   of the immutable dispatch claim must be connected to runtime DB,
   along with explicit no-retry of unknown POST outcomes.
6. Frozen +14% best-profit arming, 0.5-percentage-point short trailing,
   verified in-place native stop amendment, a secure continuous watcher,
   and owned emergency-close/reconciliation for nonflat accounts must run
   before an unattended autonomous real-money worker is acceptable.
7. GitHub CI and any changed Render deployments must pass before cutover.
   Stop, POST and collateral claims are not established by a normal
   Python wheel build or a successful read-only monitoring heartbeat.

## Operator action needed to advance independently auditable journal witness

Add only this separate env secret to the isolated trader Render worker:

```text
T100_V40_EXECUTOR_RUNTIME_DSN=postgresql://t100_live_executor_runtime:<URL-encoded-existing-runtime-password>@<internal-Postgres-host>:5432/mexc_exhaustion
```

Never use `T100_V40_SIGNAL_READER_DSN` for this. Do not put this DSN in
GitHub, terminal output or ChatGPT. Then run from the worker Render Shell:

```bash
python -m app.t100_v40_render_journal_readonly
```

Expected if truly clear and all base ACL/rows are accessible:

`V40_FIRST_POSITION_JOURNAL_EMPTY_AND_READONLY_VERIFIED`.

If status is anything else, fix the specific privilege/schema/row issue;
do not clear old journals or alter grants blindly.

**This alone does NOT allow an order.** The actual entry/recovery worker
must be implemented and proven separately. Do not switch execution flags
based on this single gate.

BOXWAVE and the old paper trader remain isolated and unchanged.

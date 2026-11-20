# T100 V40 Render trader credential cutover — 11 November 2026

**Operator preference: exactly two MEXC credentials, no fingerprint.**
Only `MEXC_API_KEY` and `MEXC_API_SECRET` are used by the dedicated
V40 trader's signed-GET preflight. They must belong to the dedicated
T100 WEB PILOT subaccount, never BOXWAVE or the older shadow account.

Changing those two values in the Render executor will change which
exchange key is presented. It DOES NOT prove account identity or flip
trading on: an independently verified account/permission/equity check
and all live safety gates must run again after every rotation.

## Current service state before operator changes

The existing `mexc-standard-short-trader` worker
(`srv-d9v1r42d0e5s73eemsv0`) was confirmed **SUSPENDED**, on
`main`, start command `python -m app.t100_200_trader`, with
auto-deploy ON. The connected Render environment update endpoint
returned **400 cannot deploy suspended service**: this means the
assistant has NOT replaced this service's existing environment,
branch, start command, or API credentials.

**Do not paste new subaccount keys into this service while the old
start command remains configured.** Do not share keys in ChatGPT,
GitHub, any logs, screenshots, or a shell command/history.

## Step 1: safely repoint the suspended existing worker

In https://dashboard.render.com/worker/srv-d9v1r42d0e5s73eemsv0

1. Disable auto-deploy.
2. Change branch to:
   `integration/t100-v40-live-signal-intake-20261108`
3. Build command: `pip install .`
4. Start command:
   `python -m app.t100_v40_render_executor_disabled_worker --run`
5. Under Environment, remove the old scanner-owner `DATABASE_URL`,
   legacy Discord webhook and any inherited environment groups carrying
   scanner, old shadow or BOXWAVE secrets. Remove any ambiguous
   `T100_V40_TRADER_API_KEY` and
   `T100_V40_TRADER_API_SECRET` values. Replace, DO NOT retain, any
   old `MEXC_API_KEY` and `MEXC_API_SECRET` values from the retired
   paper trading setup.
6. Set non-secret safety flags:

```dotenv
PYTHON_VERSION=3.12.11
T100_V40_EXECUTOR_DISABLED_WORKER=true
T100_V40_EXECUTOR_KEY_PREFLIGHT_ENABLED=true
T100_V40_ACCOUNT_PROFILE=DEDICATED_WEB_PILOT
T100_EXECUTION_MODE=paper
T100_LIVE_ORDER_API_ENABLED=false
T100_LIVE_KILL_SWITCH=true
EXECUTION_ENABLED=false
TRADING_MODE=paper
```

7. Save settings without resuming the previous build; if Render won't
   save a suspended service's settings, leave it suspended and report
   the exact failure. Do not start the old trader for the sake of a
   secret update.

## Step 2: private two-variable setup

After independently verifying the safe start command/branch:
enter through THIS Render trader service's Environment screen only:

```dotenv
MEXC_API_KEY=<dedicated T100 WEB subaccount Futures API key>
MEXC_API_SECRET=<the matching subaccount API secret>
```

There is **no SHA/fingerprint variable**. Key+secret are used for
authentication. Choose a key restricted to this subaccount, with
withdrawal privileges disabled. Review API IP restrictions and server
permissions in MEXC UI. Do not add these secrets to the scanner or
the old shadow monitor.

## Step 3: authenticated GET-only preflight, then separately certified live pilot

The new GitHub branch provides a networkless holding start runner
`app.t100_v40_render_executor_disabled_worker --run`. It never imports
the MEXC client, opens a database or sends requests, even when the two
MEXC secrets are configured.

A Render **One-Off Job** based on that reviewed new build can run:
`python -m app.t100_v40_render_executor_key_preflight --once`

That code uses two sets of five signed GETs to verify empty positions,
stops, open-order counts, hedge mode and account equity. It refuses
to proceed if the scanner-owner `DATABASE_URL`, alternate
`T100_V40_TRADER_API_KEY/SECRET` names or any incorrect paper/kill
flag is present. All POSTs, DB writes and Discord requests remain ZERO.
It cannot prove exchange-side trading authorization, account identity
or native stop lifecycle. The report's proposed notional
`min($12.50, 25% of actual available/equity)` is *research-only*.

One-off job reference: https://render.com/docs/one-off-jobs .
The current connected Render integration does not expose one-off job
creation, and the operator has not yet prepared the suspended service.

**NOT a live trading switch.** A real V40 SHORT CROSS 1x SL75 trade
still requires a fresh qualified Stage2/49-bar/VOL30 signal, exact
market lot and collateral risk, separately certified native stop
attachment/trigger and reconciliation, durable no-retry order claim,
a running independent nonflat watchdog, manual close fallback and
final explicit operator approval of the exact trade. Current general
live adapter is `NOT_IMPLEMENTED`. The other account's shadow
continues to report `NATIVE_PROTECTION_LOST`; do not decommission it.

No live order has been submitted or permission enabled by this change.

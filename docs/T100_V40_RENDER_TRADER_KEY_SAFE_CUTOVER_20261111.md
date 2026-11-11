# T100 V40 Render trader credential cutover — November 11, 2026

Operator confirmed a **Futures trading-enabled API key** exists for the
isolated T100 WEB pilot subaccount. Key has not been supplied to ChatGPT,
GitHub, Render or existing scanner by this change.

## Core protection

The current `mexc-standard-short-trader` is **SUSPENDED** and still has
the old branch (`main`), old start command
`python -m app.t100_200_trader`, and old environment. DO NOT paste
new trading credentials into it until all configuration changes below
are saved and the old database principal is removed.

The connected Render environment update returned
`400 cannot deploy suspended service`, and a follow-up `get_service`
confirmed the worker remains SUSPENDED/unchanged. No credentials,
service status, database or MEXC trading settings were changed by the
assistant. The Render Dashboard supports Environment -> **Save only**
without deployment: https://render.com/docs/configure-environment-variables .

## Step 1: only Configure suspended service, do NOT resume

Open https://dashboard.render.com/worker/srv-d9v1r42d0e5s73eemsv0

1. Under Settings select:
   - Auto-Deploy = OFF.
   - Branch = integration/t100-v40-live-signal-intake-20261108
   - Build Command = pip install .
   - Start Command =
     `python -m app.t100_v40_render_executor_disabled_worker --run`
2. On Environment, REMOVE all old `DATABASE_URL`, legacy
   `MEXC_API_KEY`, `MEXC_API_SECRET`, old paper trader Discord webhook,
   and any linked environment group exposing a database-owner login,
   shadow/BOXWAVE credentials or trading keys. Do NOT edit scanner
   `srv-d9kba2jm8hqs73c3dfi0` or old live shadow
   `srv-dbf5798m7kps73fqcuog`.
3. Set the following **non-secret** environment on THIS suspended trader
   service:

```dotenv
PYTHON_VERSION=3.12.11
LOG_LEVEL=INFO
T100_V40_EXECUTOR_DISABLED_WORKER=true
T100_V40_EXECUTOR_KEY_PREFLIGHT_ENABLED=true
T100_V40_ACCOUNT_PROFILE=DEDICATED_WEB_PILOT
T100_EXECUTION_MODE=paper
T100_LIVE_ORDER_API_ENABLED=false
T100_LIVE_KILL_SWITCH=true
EXECUTION_ENABLED=false
TRADING_MODE=paper
```

4. On Environment, choose **Save only** (NOT deploy/resume). If branch
   or start command cannot be edited while suspended, DO NOT resume the
   legacy worker blindly. Report this blocker for a safer workaround.
5. Verify the changed *non-secret* settings via connected Render API
   before provisioning any private trading key.

## Step 2: private credential enrollment AFTER settings validated

Enter ONLY through Render Environment UI for this isolated trader:

```text
T100_V40_TRADER_API_KEY=<Futures-trading-enabled key from T100 WEB pilot>
T100_V40_TRADER_API_SECRET=<matching secret from exact same subaccount>
T100_V40_TRADER_API_KEY_SHA256=<lowercase SHA256 of exact API key>
```

The key SHA256 can be calculated on the operator's Mac in a private
TTY without saving API key in command history:

```bash
python3 -c 'import getpass,hashlib; print(hashlib.sha256(getpass.getpass("Paste API key locally (not secret): ").encode().hexdigest())'
```

**Never paste keys, Render API tokens or secret values into chat or GitHub.**
Do not enable withdrawal permissions; MEXC API access may require KYC and
correct source IP allowlisting. Render default outbound IP ranges are
shared; optional dedicated outbound IPs increase operating costs.
Verify server-side account identity and Futures trading rights in MEXC
UI before ever issuing a trade permit. Read-only GET success cannot
prove order placement permission or a safe stop lifecycle.

## Step 3: initial safe code artifact and GET-only one-off probe

When the branch, disabled holding command, and isolated environment
have been verified, the worker can be built/deployed under safe flags
with the disabled holding command as its **only** service start.
This process does not import MEXC HTTP, a database or any notifier,
and prints `V40_DISABLED_EXECUTOR_HOLDING_PROCESS` while idle.
It should be suspended again when not needed to control compute cost.

Run the exact **one-off job** command only against this isolated
new-build service artifact (once its build is confirmed):

```text
python -m app.t100_v40_render_executor_key_preflight --once
```

This command makes exactly two sets of five signed GET requests to
the dedicated account for positions, native stops, open-order counts,
USDT equity and hedge mode. No POST. It refuses to run if any original
`DATABASE_URL`, `MEXC_API_KEY` or `MEXC_API_SECRET` remains or
paper/kill flags differ. In particular, it does NOT claim the server's
API trading permission is verified. Success status is
`V40_TRADING_KEY_SIGNED_GET_FLAT_VERIFIED_NONAUTHORIZING`, with proposed
research-only notional min($12.50, 25% of confirmed available/equity).
Not an executable lot, maximum loss or entry permit.

Render One-Off Jobs run from the current base service build and env and
are billed by execution duration:
https://render.com/docs/one-off-jobs . The currently connected Render
integration does not expose Create Job, only service inventory/deploy
and environment updates. Do not start a one-off job from the old
`main` build or a service inheriting the scanner's Postgres owner key.

## Later first LIVE V40 trade (NOT part of this cutover)

There is not yet a certified V40 live adapter: current
`LIVE_ADAPTER_STATUS = NOT_IMPLEMENTED`. Never flip it just to
run a BTC one-contract test. First require a fresh eligible V40
Stage2 + causal 49-bar + VOL30 signal, exact executable lot, SHORT
CROSS 1x margin/tier/collateral, durable pre-POST no-retry claim,
attached native whole-position MARKET SL75 with verified reference,
independent nonflat account monitoring and stop readback, manual
close fallback, and operator signoff on symbol/size/risk.
Old shadow protects ANOTHER account, as reported by the operator,
and remains active until that account's exposure is reconciled.

User-facing status on this document's creation: **NO Render
environment or service modification succeeded; no credentials
transferred; no MEXC order submitted; no production authorization.**

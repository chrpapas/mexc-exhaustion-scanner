# T100/225 V40 isolated CROSS pilot GET-only watchdog — Mac candidate (2026-11-11)

**NOT DEPLOYED, NOT LIVE TRADING AUTHORIZATION, NOT A CERTIFIED LOSS CAP.** Separate from BOXWAVE, original T100 trader, Render services and production DB. No MEXC POST paths exist in these modules; Discord webhooks may receive redacted notifications only. Neither the ChatGPT/GitHub hourly watch nor this local candidate can guarantee that an exchange-native stop will fill during a fast market.

## Why this is separate

The old `app.t100_pilot_watchdog_service` and `app.t100_pilot_watchdog_observer` explicitly require BTC_USDT, ISOLATED one contract and should **not** be reused for live V40 CROSS. The old Mac LaunchAgent watch/sentinel verified a flat-account observation once with slower default intervals; it cannot protect an intentional nonflat V40 position.

This candidate adds:

- `app.t100_v40_cross_watchdog_observer`: two signed GET inventory bookends, validated hedge mode, open order count/stop count reconciliation, and exact **one owned** SHORT, CROSS=2, leverage=1, entry order/fills, quantity, native **full-volume MARKET SL** with expected immutable IDs and reviewed maximum stop price. No adoption or automatic stop-ID rotation. An owned position that disappears is **ALERT/UNKNOWN**, not proof of a safe close.
- `app.t100_v40_cross_watchdog_service`: strict allowlisted signed GET transport; macOS Keychain secrets; account API key fingerprint pin; immutable scope hash; 0600 durable SQLite alarm outbox; a one-second *delay after each completed poll*; separate Discord-only critical alarm notifier. A full observation involves 8 GETs while flat or 10 GETs while owned. **The actual observation duration and API rate limits must be measured on the Mac**. Do not market the interval as hard real-time or claim sub-five-second successful sampling without measured evidence.
- `app.t100_v40_cross_watchdog_sentinel_service`: separate local process, no MEXC key or exchange calls, checks journal freshness every five seconds, alarms on >30s stale journal, missing watcher/latch, with **different** webhook and separate alert-outbox journal. Both on one Mac are a *common-host failure domain*. They can stop together if the Mac sleeps, loses connectivity, shuts down or logs out.
- Tests are **offline mocked**; no network, venue mutation or production deployment.

## Required before any real run

1. Create a dedicated **exchange-side READ-ONLY** MEXC Futures API key for the exact T100 WEB pilot subaccount. It is not the trade-enabled Keychain key and never belongs to BOXWAVE or the main account. Confirm server-side access in MEXC UI; API key hash equality does NOT prove subaccount identity or permission type. Do not paste a key into chat.
2. In macOS **Keychain Access**, create these **generic password** items with the exact service/item names and accounts (values are secrets, not examples to share):
   - Service `t100225-mexc-web-pilot-readonly-v1`: account `MEXC_API_KEY` with the read-only key and account `MEXC_API_SECRET` with its secret.
   - Service `t100225-v40-watchdog-discord-v1`: account `DISCORD_WEBHOOK_URL` with a **new** Discord webhook for the primary observer.
   - Service `t100225-v40-watchdog-sentinel-discord-v1`: account `DISCORD_WEBHOOK_URL` with a **different** webhook for the sentinel. Both must differ from the trader's webhook.
3. Confirm the target subaccount is *flat* before initial `FLAT` mode. No manual orders, unrelated positions or outstanding stops. Do NOT place an order merely to test observer startup. Keep T100 paper/kill switches.
4. Both services must be tested supervised, with an alarm-delivery drill and deliberate process-death drill. Mac LaunchAgents/always-on supervisor and independent host delivery are NOT installed by this candidate.

## Optional: one-command secure Keychain setup for missing items

When all four new credential **values** are ready, run the interactive
local-only installer:

```bash
.venv/bin/python -m scripts.t100_v40_watchdog_keychain_setup --install
```

Type `YES` to opt in. For each **missing** Keychain generic-password item,
macOS `/usr/bin/security add-generic-password` is called with `-w` as
the final argument and **without a password argument**, so the native
Mac prompt accepts the value securely rather than putting it in command
history, process command-line arguments, GitHub or ChatGPT. Existing
entries are NEVER overwritten. This makes no network requests and does
not change MEXC permission settings. The local script prints the same
redacted four-item readiness result at the end.

**Do not** type a trading-enabled key into these read-only watchdog
items. If the MEXC account page does not let you grant only Futures
read/trading-information read permissions, stop and do not use a writable
key as a substitute.

## First, one no-network local prerequisite check

Before configuring any runtime or creating a journal, simply run in the
repository root after syncing the branch:

```bash
.venv/bin/python -m scripts.t100_v40_watchdog_local_prerequisites
```

It reports whether the **dedicated** read-only MEXC key pair and both
different Discord webhooks are stored in their exact macOS Keychain
services, without printing raw keys or webhook URLs. `LOCAL_SECRETS_PRESENT`
does **not** verify MEXC exchange-side read-only permissions, account
identity, alerts, network access or operational watchdog status. If
`LOCAL_SECRETS_SETUP_REQUIRED`, complete the Keychain steps above first.

## Safe first live check: signed GETs, no alarm journal

Once the local Keychain check reports `V40_WATCHDOG_LOCAL_SECRETS_PRESENT_NONAUTHORIZING`, use this **before** running the persistent watchdog. This requires no Discord sends, creates no journal and makes no database changes. It performs eight signed GET-only queries on the dedicated read-only T100 WEB pilot credentials, if the paper-mode gates pass:

```bash
export T100_EXECUTION_MODE=paper
export T100_LIVE_ORDER_API_ENABLED=false
export T100_LIVE_KILL_SWITCH=true

.venv/bin/python -m scripts.t100_v40_watchdog_flat_get_only_preflight \
  --keychain-service t100225-mexc-web-pilot-readonly-v1
```

Expected on a **currently empty** account:
`WATCH_FLAT_INVENTORY_OBSERVED_NONAUTHORIZING` with
`flat_inventory_observed=true`.
If the GET key is denied, the account is not actually flat, inventory is
unreliable, or hedge mode differs, it **fails closed**; do not force/erase
state or use a writable API key. This is NOT exchange-side permission
certification or independent subaccount identity verification; verify those
in the authenticated MEXC subaccount API UI separately. A GET-only client's
lack of a POST method does not prove that its API key is read-only.

## Supervised flat-account dry run

Use the integration branch as checkout, and `cd` to repo root. Commands below avoid putting raw MEXC keys or webhooks in shell history.

```bash
git switch integration/t100-v40-live-signal-intake-20261108
git pull --ff-only origin integration/t100-v40-live-signal-intake-20261108
umask 077
mkdir -p "$HOME/Library/Application Support/t100-v40-pilot"
chmod 700 "$HOME/Library/Application Support/t100-v40-pilot"
export T100_EXECUTION_MODE=paper
export T100_LIVE_ORDER_API_ENABLED=false
export T100_LIVE_KILL_SWITCH=true
export T100_V40_WATCH_ENABLED=true
export T100_V40_WATCH_MODE=FLAT
export T100_V40_WATCH_KEYCHAIN_SERVICE=t100225-mexc-web-pilot-readonly-v1
export T100_V40_WATCH_DISCORD_KEYCHAIN_SERVICE=t100225-v40-watchdog-discord-v1
export T100_V40_WATCH_JOURNAL="$HOME/Library/Application Support/t100-v40-pilot/flat-watch.sqlite"

# Local fingerprint from Keychain, never printing the API key itself.
export T100_V40_WATCH_API_KEY_SHA256="$(
  python3 -c 'import hashlib,subprocess; k=subprocess.check_output([
    "/usr/bin/security","find-generic-password","-s",
    "t100225-mexc-web-pilot-readonly-v1","-a","MEXC_API_KEY","-w"
  ]).strip(); print(hashlib.sha256(k).hexdigest())'
)"

# No network or journal changes; use returned SHA to pin the sentinel.
.venv/bin/python -m app.t100_v40_cross_watchdog_service --print-sentinel-scope

# First live, signed GET-only FLAT inventory and durable journal/alert test.
.venv/bin/python -m app.t100_v40_cross_watchdog_service --cycles 1
```

Expected when flat and reachable: `WATCH_FLAT_INVENTORY_OBSERVED_NONAUTHORIZING`. If account is not flat, the mode is wrong, or GETs fail, expect a **latched alarm**, not permission to trade. Never delete/overwrite a latched journal just to obtain green. A fresh, explicitly scoped and independently reviewed test journal is needed for a new drill.

To run supervised continuous primary observations, in Terminal A with the *same exported variables*:

```bash
.venv/bin/python -m app.t100_v40_cross_watchdog_service --continuous
```

In a separate Terminal B, after importing the non-secret base/scope variables (not source the trading environment), use the exact printed sentinel scope and:

```bash
export T100_EXECUTION_MODE=paper
export T100_LIVE_ORDER_API_ENABLED=false
export T100_LIVE_KILL_SWITCH=true
export T100_V40_SENTINEL_ENABLED=true
export T100_V40_SENTINEL_DISCORD_KEYCHAIN_SERVICE=t100225-v40-watchdog-sentinel-discord-v1
export T100_V40_SENTINEL_SCOPE_SHA256='<paste local scope SHA, NOT a secret>'
export T100_V40_SENTINEL_WATCHDOG_JOURNAL="$HOME/Library/Application Support/t100-v40-pilot/flat-watch.sqlite"
export T100_V40_SENTINEL_OUTBOX_JOURNAL="$HOME/Library/Application Support/t100-v40-pilot/flat-sentinel.sqlite"
.venv/bin/python -m app.t100_v40_cross_watchdog_sentinel_service --continuous
```

The sentinel must be a **distinct process**, configured with the correct scope. An alarm may remain latched after a deliberate failure; do not silently erase it. For a real service, use reviewed process supervision/LaunchAgents and prevent Mac sleep as appropriate, then repeat long enough to record actual poll duration and rate-limits. A Mac that sleeps is not an unattended-safe monitor.

## OWNED mode (not eligible yet)

OWNED requires **reviewed independently durable** evidence of exact V40 signal-entry identity, matching account/read-only key, `EXAMPLE_USDT`-style non-BTC/ETH symbol, venue `positionId`, entry `externalOid`, entry order ID, complete filled contract quantity, active native stop ID and maximum reviewed SL price. Require a **new, separate journal** and new scope SHA. The observer verifies current MEXC CROSS=2, 1x, full-position MARKET SL and one owned position. If a stop ID changes after an amendment, it latches an alarm and **never auto-adopts** a replacement.

Configuration variables added to the common paper-only environment for the future reviewed OWNED process:

```bash
export T100_V40_WATCH_MODE=OWNED
export T100_V40_WATCH_SYMBOL='<exact reviewed V40 symbol>'
export T100_V40_WATCH_POSITION_ID='<exact reviewed venue position ID>'
export T100_V40_WATCH_ORIGIN_OID='<durable T100 entry externalOid>'
export T100_V40_WATCH_ENTRY_ORDER_ID='<reviewed filled exchange entry order ID>'
export T100_V40_WATCH_STOP_ID='<reviewed active native stop ID>'
export T100_V40_WATCH_CONTRACTS='<exact filled contract quantity>'
export T100_V40_WATCH_MAX_STOP_PRICE='<reviewed maximum short SL price>'
export T100_V40_WATCH_JOURNAL="$HOME/Library/Application Support/t100-v40-pilot/owned-watch.sqlite"
```

These placeholders are **not** a run instruction before a supervised full stop/entry lifecycle. The current candidate monitors only ONE owned CROSS position, not four simultaneously; it does not read a durable DB checkpoint itself and does not prove true provenance beyond pinned IDs and live GETs. There is an unavoidable monitoring gap when going from pre-entry FLAT to post-entry OWNED without a separately certified atomic/supervised handoff. **Do not place a real V40 order relying on this candidate as continuous protection.**

## Must be verified on the actual Mac before any operational certification

- Exactly the dedicated T100 read-only key and proper subaccount, distinct from BOXWAVE/trader, with no exchange-side POST permission.
- Supervised real GET sampling and measured timestamp/revision cadence under authentic API rate limits, no unexpected gaps, journal persists a stop-watch restart and does not silently clear alarms.
- Two separate actual Discord message deliveries for a deliberate safe no-trade failure/drill, persistent failure when network unavailable, restart/sentinel-death drill with zero MEXC mutations.
- One owned CROSS 1x position with independently pinned durable checkpoint and active stop, no manual positions, full exact volume, verified stop replacement and economic risk limits. This requires a separately authorized *supervised* pilot order with exchange-native protection and recovery. Existing BTC isolated 1-contract evidence is not V40 CROSS certification.
- Independent host/scheduler liveness and delivery. Local Mac dual processes alone are not operational redundancy.

**All flags remain false:** `live_authorization`, `entry_allowed`, `stop_execution_certified`, `continuous_protection_certified`. No MEXC POST, Render or PostgreSQL mutation, and no BOXWAVE action is performed by these modules.

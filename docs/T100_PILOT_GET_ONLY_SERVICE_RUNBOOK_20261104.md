# T100 isolated GET-only pilot service and external heartbeat (2026-11-04)

**Engineering candidate, never deployed. NOT a production watchdog, not a loss
cap, and NOT an emergency-close executor.** All exchange access in this
candidate is via HTTP GET. Notifications may POST **to Discord only**.
The original trader, BOXWAVE, main account, Render services, production DB and
credentials were not modified.

## Implementation

- `app/t100_pilot_watchdog_service.py`: explicit Python process entrypoint.
  Instantiates `SignedGetOnlyMexc` (a hard path-allowlisted GET-only
  `httpx.AsyncClient` wrapper, **not** `MexcTradeClient`) and the already
  strict `ReadOnlyMexcProbe` + `observe_pilot_watch_get` snapshot verifier.
  Runs `PilotWatchdogRunner` with a durable, scope-bound SQLite alarm outbox.
  Alerts have redacted WATCH_* descriptions, delivered using a **separate**
  Discord webhook and existing notification-only sender. It cannot place,
  cancel or amend MEXC orders and does not import their transport methods.
- `app/t100_pilot_watchdog_heartbeat.py`: a second, independent command that
  opens the watchdog journal SQLite **query-only, read-only**; no file creation.
  It checks exact scope fingerprint, last recorded observation, revision,
  pending alarm messages, the persistent alarm latch and elapsed age. It
  prints a fixed status and returns exit code 0 (recent nonauthorizing
  observation, no alarm) or 2 (missing/stale/unknown/latched).
- `tests/test_t100_pilot_watchdog_service.py` and
  `tests/test_t100_pilot_watchdog_heartbeat.py` cover pinned credentials,
  paper/kill controls, signed GET allowlist, redaction, mock Discord delivery,
  clock skew, stale journal and an actual second-process exit-code test.

## Explicit configuration, not performed by engineering in chat

Provision a **separate exchange-side READ-ONLY API key** restricted to the
intended API-only pilot subaccount. Do not reuse main account trading keys,
paste credentials into chat, or deploy these credentials into the existing
trader. The service cannot verify server-side API permission restrictions and
its API-key SHA-256 pin alone does **not** certify account/subaccount identity.

Use a *private secrets store* to inject exactly:

- `T100_EXECUTION_MODE=paper`
- `T100_LIVE_ORDER_API_ENABLED=false`
- `T100_LIVE_KILL_SWITCH=true`
- `T100_PILOT_READONLY_API_KEY` and `T100_PILOT_READONLY_API_SECRET`
- `T100_PILOT_EXPECTED_API_KEY_SHA256` — compute locally from the exact key.
  The service checks its equality before creating the network client.
- `T100_PILOT_ORIGIN_OID` — the exact **already committed** one-contract
  `BTC_USDT` entry external ID. Must not be inferred from other account orders.
- `T100_PILOT_STOP_ID` — exact audited native stop ID, if already known.
- `T100_PILOT_MAX_STOP_PRICE` — operator-reviewed positive short-stop ceiling.
- `T100_PILOT_WATCHDOG_JOURNAL` — absolute local path in a private,
  persistent directory, never `:memory:`, not shared across hosts.
- `T100_PILOT_DISCORD_WEBHOOK_URL` — **separate** Discord webhook, not
  the current main trader webhook. Required even for one-shot service.

Do not store raw credentials or the webhook in Git, logs, shell history,
example docs or chat. Read-only key access must be independently confirmed.

## Commands to review, not executed here

From the repository root, after the reviewed dependencies and private config
are available:

```bash
python3 -m app.t100_pilot_watchdog_service --cycles 1
# For a separate operator-supervised process only:
python3 -m app.t100_pilot_watchdog_service --continuous
# A different process or host MUST separately schedule this read-only check:
python3 -m app.t100_pilot_watchdog_heartbeat \
  --journal /absolute/private/path/to/pilot.sqlite \
  --expected-scope-sha256 <locally-calculated-binding-hash> \
  --stale-after-ms 30000
```

The account-scope fingerprint and the **journal scope hash** are different:
the latter binds key-hash, entry-origin, stop ID, reviewed max stop price,
symbol and one-contract scope. It is computed internally and is not printed.
A reviewed deployment must provide the exact independently calculated
journal scope hash to the heartbeat sentinel; do not skip or guess it.

`--cycles 1` exits **2** if a latched alarm or undelivered notification
exists; it exits 0 otherwise. It does not clear historical alarms. The
`--continuous` mode does not make itself a managed service; its death
requires a **separately scheduled sentinel and alert delivery**.

## Known blockers, no live-readiness promotion

1. The service has not run on Mac/Render with real MEXC GETs or actually sent
   a Discord alert. All integration tests currently mock the venue and
   notifier; the independent heartbeat CLI has a real subprocess unit test.
2. A stop ID is not known until after native stop placement. The service
   deliberately fails closed while stop ID is unknown, and the existing
   single-scope journal cannot silently adopt a later ID or reset its alarm
   latch. A separately reviewed scoped transition is necessary before
   the entire entry-to-protection interval can be supervised.
3. The service's HTTP GETs are signed but still non-atomic; a green snapshot
   does not prove continuous protection. Under poor network conditions its
   10 GET requests per observation may exceed its timeout or any API rate
   limit. This requires measuring and calibrating on the exact pilot.
4. A one-process SQLite `flock` and a separately invoked heartbeat check are
   not cross-host fencing, automatic repair or guaranteed alarm delivery.
   The heartbeat inspector has no Discord transport or scheduler of its own.
5. **No automatic close is possible.** The watchdog only raises alarms.
   Independent exchange close transport, durable request idempotency, partial
   fills, manual intervention/recovery, loss budget, live stop amendment and
   actual stop-trigger execution are all still unverified.

Until these are reviewed and tested: `entry_allowed=false`,
`live_authorization=false`, no production POST, no Render deploy.

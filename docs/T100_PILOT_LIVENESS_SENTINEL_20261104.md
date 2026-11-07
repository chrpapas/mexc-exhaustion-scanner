# T100/V40 — independent pilot watchdog death-alert sentinel (2026-11-04)

**Status: development-only; mocked integration tests passed, NOT deployed.**
This is a second process that monitors the existence, freshness and safety
state of the primary GET-only pilot watchdog's persistent SQLite journal.
Its purpose is to report a failure of the watcher itself, even if the watcher
can no longer send Discord messages. It has no MEXC credentials and cannot
open, amend or close futures orders.

## Architecture and ownership

1. Primary pilot process:
   `app/t100_pilot_watchdog_service.py --continuous` (not yet deployed)
   signs allowlisted MEXC GET requests only, verifies an explicitly owned
   BTC_USDT one-contract short/native stop, and persists `WATCH_*` observations
   and alerts to a local durable journal.
2. Independent sentinel:
   `app/t100_pilot_watchdog_sentinel_cli.py --continuous` (not yet deployed)
   reads that journal in SQLite query-only mode via
   `inspect_watchdog_heartbeat` and raises a distinct `WATCH_HEARTBEAT_*`
   alarm when the watcher journal is missing, invalid, not started, stale,
   scoped incorrectly or retains a prior alarm.
3. The sentinel writes only its **own** separate full-synchronous SQLite
   alert outbox through `PilotLivenessSentinel`. It commits a pending
   notification before calling a separate Discord-only notifier. Failure or
   restart leaves the alert pending. Duplicate delivery is possible after
   an ACK-but-before-commit crash (at-least-once, never exactly-once).
4. A stale watcher normally produces a sentinel alarm after a configurable
   30-second maximum age. The sentinel checks every 5 seconds once started.
   This delay is a *detection setting*, not a protective stop deadline,
   realized-loss bound or guaranteed maximum notification latency.
5. The sentinel itself is not independently watched. A third-party service
   supervisor/heartbeat alert is still required. Multiple Render replicas
   and distributed journal sharing are NOT supported by the local flock.

## Tests and fail-closed behavior

Files:
- `app/t100_pilot_watchdog_sentinel.py` — pure stdlib persistence/policy.
- `app/t100_pilot_watchdog_sentinel_cli.py` — explicit process and
  isolated redacted Discord-notifier wiring; does not import MEXC transport.
- `tests/test_t100_pilot_watchdog_sentinel.py` — missing/dead watcher,
  healthy read-only heartbeat, notification failure and restart, deduplication,
  recovery-without-latch-reset, clock skew, scope binding, corrupted old
  outbox, symlink/lock refusal and redacted exceptional inspector.
- `tests/test_t100_pilot_watchdog_sentinel_cli.py` — paper/kill controls,
  independent webhook constraints and **mocked** Discord delivery.
- The explicit T100 safety workflow compiles the modules and runs core
  sentinel tests with pure Python, while the complete offline pytest
  workflow also executes the HTTP-dependent CLI test.

A successful recent heartbeat returns
`WATCH_HEARTBEAT_RECENT_NONAUTHORIZING` with no new alarm, but does NOT
certify continuous native stop protection. Failed inspection returns a
redacted `WATCH_SENTINEL_HEARTBEAT_INSPECTION_FAILED` alarm. The sentinel
never clears its persistent alarm latch when the watched process recovers.

## Setup requirements — NOT executed yet

A separate supervised runtime, persistent private directory, and **new,
dedicated Discord webhook** are required. Do not reuse the existing trader's
or primary pilot watchdog's Discord webhook. No MEXC secret is required by
the sentinel. The following are required private environment settings:

- `T100_EXECUTION_MODE=paper`
- `T100_LIVE_ORDER_API_ENABLED=false`
- `T100_LIVE_KILL_SWITCH=true`
- `T100_PILOT_SENTINEL_WATCHDOG_JOURNAL`: absolute path to the actual
  existing primary watchdog SQLite file.
- `T100_PILOT_SENTINEL_ALERT_JOURNAL`: **different** absolute file path
  on durable private storage for the sentinel's own outbox. Never delete
  it merely to clear an alarm.
- `T100_PILOT_SENTINEL_EXPECTED_SCOPE_SHA256`: exact hash obtained on the
  primary watcher host via its reviewed local-only
  `python3 -m app.t100_pilot_watchdog_service --print-sentinel-scope`
  command. This is a binding, **not** a credential or proof the API key
  really has read-only exchange permissions.
- `T100_PILOT_SENTINEL_DISCORD_WEBHOOK_URL`: dedicated Discord URL scoped
  to this sentinel's liveness alerts.

After separate operator security/credential review and process supervision:

```bash
# A one-shot read-only health inspection and durable alert attempt:
python3 -m app.t100_pilot_watchdog_sentinel_cli --cycles 1

# In a separately managed, continuously running process only:
python3 -m app.t100_pilot_watchdog_sentinel_cli --continuous
```

A one-shot run exits 2 if a liveness alert remains latched or a notification
is pending, even if the most recent watched heartbeat has recovered; otherwise
it exits 0. The sentinel uses HTTP POST only toward the configured Discord
webhook. It cannot submit any MEXC order or change an MEXC position.

## Open blockers (not a deployment authorization)

- No actual MEXC read-only API observation by the primary new service.
- No actual Discord delivery by either the primary watchdog or sentinel.
- No independent supervisor ensuring that the sentinel itself is alive.
- No reviewed operational alarm acknowledgement/reset procedure; do not
  delete SQLite files as a shortcut.
- No durable exchange emergency-close executor, no quantified stop gap, no
  real stop amendment/trigger trial, and no four-slot CROSS runtime proof.
- No independent review of the full service, secrets boundary and rollout.
- No current Render deployment change has been made.

These remain release blockers for G5/G6/G9. No live trading enablement
follows from passing the isolated offline tests.

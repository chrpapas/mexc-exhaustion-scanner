# T100/225 dedicated pilot — Mac LaunchAgents first running cycle (2026-11-04)

**Source of evidence:** operator-redacted macOS Terminal outputs supplied in
ChatGPT. Not an independently instrumented Mac inspection, code-signing
attestation, longitudinal soak, or account-risk certification.

## Installation result

The earlier installer versions failed respectively with
`KEYCHAIN_STORE_READBACK_FAILED` and
`SENTINEL_WEBHOOK_NOT_DISTINCT`. The corrected local
`t100_pilot_mac_monitor_ready.py` installer ran on the operator's Mac:

```text
PILOT_KEYCHAIN=FOUND. ONLY MEXC SIGNED GET REQUESTS ARE IMPLEMENTED.
DISCORD_INSTALL_TEST=HTTP_ACCEPTED
MAC_WATCH_AGENT=START_REQUEST_ACCEPTED
MAC_SENTINEL_AGENT=START_REQUEST_ACCEPTED
DISCORD_NOTIFICATION_CHANNELS=ONE_SHARED_WEBHOOK
MEXC_MUTATIONS=ZERO LIVE_AUTHORIZATION=FALSE
```

The operator then executed `python3 ~/Downloads/t100_pilot_mac_monitor_ready.py
status` and reported:

```text
WATCH_AGENT=LOADED
WATCH_STATE=PILOT_FLAT_CONFIRMED
WATCH_REVISION=1 AGE_SECONDS=6 ALARM_LATCHED=FALSE UNDELIVERED=0
SENTINEL_AGENT=LOADED
SENTINEL_STATE=WATCHER_RECENT_FLAT_NONAUTHORIZING
SENTINEL_REVISION=1 AGE_SECONDS=5 ALARM_LATCHED=FALSE UNDELIVERED=0
MEXC_MUTATIONS=ZERO LIVE_AUTHORIZATION=FALSE
```

This **establishes one successful running cycle** reported by each of the
Mac user-level LaunchAgents, with recent journal timestamps, and the pilot
flat-account inventory verdict. The local installer had a successful
Discord HTTP test; a separate earlier one-shot pilot smoke message was
independently confirmed visible in Discord. The unattended Mac monitors'
**actual anomaly-alert delivery** has NOT yet been independently witnessed.

## Scope and safety

The installed Mac monitor code is **a flat-account anomaly detector**, not
a full native-stop protective watchdog for an intentionally opened position:
- Primary `watch` process uses dedicated pilot Keychain credentials to
  perform two bookended snapshots (three allowlisted signed MEXC GETs per
  snapshot) every 30 seconds by default. It alarms on positions, extra
  orders, stops, uncertain data or inconsistent snapshots.
- Sentinel `sentinel` uses a separate on-disk SQLite alarm journal to
  watch the primary journal every 10 seconds. A 90-second-old heartbeat
  produces a liveness alert. These are configured defaults and **not a
  maximum-loss or real-time protection guarantee**.
- Each program runs under user-level macOS launchd, so both may be offline
  when the Mac is off, logged out, asleep, without Internet, or the host
  Keychain is inaccessible.
- Their Discord delivery is through **one shared webhook**, not two
  independently provisioned channels. Loss of this webhook or Discord
  simultaneously affects both notification paths.
- Alarm latches are durable and don't silently reset on one healthy cycle.
  They are not evidence that an exchange position was closed.
- The script explicitly does not implement MEXC POST, automatic emergency
  closes, stop amendments or triggering a stop. Its existing live-trading
  authorization flag is false.

## Required evidence to advance G6

1. Watch and sentinel revisions increasing over time with both ages below
   their respective thresholds on the exact installed version.
2. Receipt of an **unattended, deliberately generated non-trading alert**
   in Discord, with exact redacted status, without modifying MEXC orders.
3. Scoped restart/freeze/failure drill on the *actual Mac* with safe recovery
   and no stale hidden alarms, plus alert-delivery failure replay if possible.
4. A genuinely independent host/channel and supervised liveness for the
   sentinel itself; local dual LaunchAgents are not redundant hosts.
5. Before any non-flat pilot, scope and ownership checks for one native stop,
   independently validated stop activation, emergency close and audited
   loss controls. Existing flat-account monitor is insufficient for that.

**Gate status unchanged:** G6 operationally NOT CERTIFIED; G5/G9 BLOCKED.
No production release, no Render or BOXWAVE changes, no new orders.

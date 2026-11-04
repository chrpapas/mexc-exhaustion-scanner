# T100/225 pilot Mac — sentinel-only real launchd crash drill

**Status:** prepared, reviewed with 11 local offline tests, **NOT yet run on the operator's Mac**. This is a real-process failure test; it does not touch MEXC orders and does not restart the primary GET-only watcher.

## Why sentinel-only first?

The primary flat-account watcher and sentinel use sticky alarm latches.
Intentionally suspending the **primary** past its 90-second heartbeat
deadline could leave a permanently latched sentinel alert that requires
an audited acknowledgment procedure. We must not accidentally clear a
real protection-loss alert to make a drill green.

Instead, first verify macOS's automatic process-supervision capability
on the **sentinel itself**, while the live GET-only watcher continues.

## Safety and operations

Download `scripts/t100_pilot_mac_sentinel_restart_drill.py` from this
development branch onto the same Mac and run locally:

```bash
python3 ~/Downloads/t100_pilot_mac_sentinel_restart_drill.py
```

The script:
1. Checks macOS, verifies the installed `watch` and `sentinel`
   LaunchAgent plists have the exact expected labels and program arguments
   and are configured with `KeepAlive=true`.
2. Checks both agent processes currently have discoverable PIDs and both
   journals have a recent, correct scoped binding, confirmed-flat status,
   no sticky latch, and no pending notifications.
3. Signals **only** the sentinel service using
   `launchctl kill SIGKILL gui/<uid>/com.t100225.pilot.getonly.sentinel`.
   The primary MEXC observer is **not stopped**.
4. Waits at most 45 seconds for launchd to create a **different sentinel
   process PID**, and for the sentinel journal revision to increase with
   a healthy recent state.
5. On failure to verify autorestart, issues one bounded
   `launchctl kickstart` attempt for **the sentinel only**, reports
   failure and leaves any alarm latches untouched.

The script does not read API keys, contact the MEXC or Discord APIs,
write the installed SQLite journals or change trading permissions. It
does temporarily interrupt sentinel coverage during its process crash;
the watcher itself continues to poll MEXC.

## Evidence needed

The successful expected output is:

```text
PRECHECK=FLAT_BOTH_AGENTS_HEALTHY
TARGET=ONLY_SENTINEL_LAUNCHAGENT
SENTINEL_SIGNAL=SIGKILL_ACCEPTED
SENTINEL_AUTO_RESTART=PASS_NEW_PROCESS_AND_FRESH_JOURNAL
ALARM_LATCHES=NONE_BEFORE_OR_AFTER
INSTALLED_JOURNALS=READ_ONLY_BY_DRILL
MEXC_REQUESTS_BY_DRILL=ZERO MEXC_MUTATIONS=ZERO LIVE_AUTHORIZATION=FALSE
```

Do not present this as passed until the operator runs the real Mac
command and reports the output. To verify no monitoring regression, also
run the existing `t100_pilot_mac_monitor_ready.py status` command
after the drill.

The earlier real Mac alert-path drill provided watcher 350→351 and
sentinel 1118→1122, with two Discord HTTP accepted/acknowledged
nontrading drill alerts. Human confirmation that **both** new messages
are visible in the Discord channel is not yet documented. This
sentinel-restart drill does not test real Discord delivery.

## Remaining controls

G6 is not certified: real installed primary watcher fault-injection,
loss-of-host coverage, second host/channel redundancy, emergency close
and nonflat stop-loss monitoring remain unverified. G5/G9 remain blocked.
No update here permits live orders, Render changes, or BOXWAVE changes.

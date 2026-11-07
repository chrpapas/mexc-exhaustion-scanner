# T100/225 pilot — operator-run Mac alert drill evidence (2026-11-04)

**Evidence provenance:** redacted operator-provided Terminal output from
`t100_pilot_mac_alert_drill.py` on the Mac. This note reflects reported
results; it is not independently instrumented host access. The test did
not modify the live monitoring journals or transmit MEXC requests.

## Operator-reported output

```text
INSTALLED_AGENTS=LOADED_HEALTHY
MONITOR_INITIAL_REVISIONS=WATCH_350_SENTINEL_1118
MONITOR_CONTINUITY=PASS_WATCH_AND_SENTINEL
MONITOR_FINAL_REVISIONS=WATCH_351_SENTINEL_1122
DISCORD_WATCH_ALERT=HTTP_ACCEPTED_OUTBOX_ACKED
DISCORD_SENTINEL_ALERT=HTTP_ACCEPTED_OUTBOX_ACKED
PRODUCTION_MONITOR_JOURNALS=UNMODIFIED_BY_DRILL
MEXC_REQUESTS_BY_DRILL=ZERO MEXC_MUTATIONS=ZERO LIVE_AUTHORIZATION=FALSE
DISCORD_CHANNEL_VISIBILITY=OPERATOR_CONFIRMATION_REQUIRED
```

## Interpretation

- Both installed macOS LaunchAgents were **loaded and healthy** at the
  time of the drill.
- The actual running watcher journal revision increased **350 → 351**,
  and the independent sentinel journal revision increased **1118 → 1122**
  during the measured interval. This is multiple-cycle operational
  continuity evidence, not a guaranteed future uptime or complete soak.
- The drill used **separate disposable SQLite alert journals** to
  exercise watcher and sentinel mock-alert delivery through the real
  Discord notification code. Both Discord webhook calls were accepted
  and both durable outbox records were acknowledged.
- The installed production monitor journals were reported **unmodified
  by the drill**. It sent **zero MEXC GETs or order mutations** itself.
  The separate always-on observer may continue its own GET polling.
- **Human confirmation that BOTH new test messages actually appeared
  in the Discord channel is still pending.** HTTP acceptance is not
  the same as operator-observed delivery. Do not mark this satisfied
  without a separate operator confirmation.
- This drill does not SIGSTOP/SIGKILL or alter the *installed* monitors.
  An earlier offline CI process-death drill used separate simulated
  processes, not the actual Mac LaunchAgents.
- Watcher and sentinel share a **single Discord webhook** and the same
  Mac host. The sentinel does not survive loss of that host or webhook.
  The flat-account monitor is **not an emergency-close executor** and
  does not certify stop amendments, native-stop execution, four-slot
  CROSS margin or production live authorization.

**G6 remains NOT CERTIFIED**, although continuous Mac polling and real
watcher/sentinel Discord webhook acceptance/outbox acknowledgments are now
observed. G5/G9 remain BLOCKED; `entry_allowed=false`,
`live_authorization=false`; no production deployment or order permissions.

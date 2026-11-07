T100/225 — safe macOS live alert-delivery drill, 2026-11-04

INSTALL NOTHING. Keep the existing two pilot LaunchAgents running.
This separate script does not write to or restart those services.

1. Download t100_pilot_mac_alert_drill.py into ~/Downloads.
2. Run:

   python3 ~/Downloads/t100_pilot_mac_alert_drill.py

Expected output when both monitors remain healthy and Discord accepts two
simulated alerts:

   INSTALLED_AGENTS=LOADED_HEALTHY
   MONITOR_INITIAL_REVISIONS=WATCH_<n>_SENTINEL_<n>
   MONITOR_CONTINUITY=PASS_WATCH_AND_SENTINEL
   MONITOR_FINAL_REVISIONS=WATCH_<larger>_SENTINEL_<larger>
   DISCORD_WATCH_ALERT=HTTP_ACCEPTED_OUTBOX_ACKED
   DISCORD_SENTINEL_ALERT=HTTP_ACCEPTED_OUTBOX_ACKED
   PRODUCTION_MONITOR_JOURNALS=UNMODIFIED_BY_DRILL
   MEXC_REQUESTS_BY_DRILL=ZERO MEXC_MUTATIONS=ZERO LIVE_AUTHORIZATION=FALSE
   DISCORD_CHANNEL_VISIBILITY=OPERATOR_CONFIRMATION_REQUIRED

Check Discord for TWO messages:
   T100_PILOT_WATCH DRILL_ONLY_WATCH_ALERT_TEST . NO MEXC ORDERS
   T100_PILOT_SENTINEL DRILL_ONLY_SENTINEL_ALERT_TEST . NO MEXC ORDERS

The test does not inject faults into the installed monitors or the MEXC
account. It uses temporary, private SQLite journals to simulate alarm and
failed delivery, reopen the journal, retry the same alarm over the real
webhook, and confirm it is acknowledged with no duplicated resend. The
script performs no MEXC HTTP requests, calls no trading code, and prints
no API keys or webhook URLs. It reads the existing monitor journal states
without modifying them. Both agents must remain loaded and healthy.

Default continuity observation window: 42 seconds. If running on macOS in
an unusual low-power state and revisions do not advance, the script stops
without sending Discord alerts. Do NOT reset production journals to
clear an alarm. The script will print T100_DRILL_HALTED=<FIXED_CODE>.

The two notifications may share the same webhook and are not independent
channel or host redundancy. This is an operational delivery-path test, not
proof of emergency close, stop execution, or approval to trade live.
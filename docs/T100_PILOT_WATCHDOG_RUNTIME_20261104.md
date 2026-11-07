# T100 pilot — durable GET-only watchdog loop candidate (2026-11-04)

Status: **OFFLINE-TESTED HARNESS; NOT DEPLOYED OR OPERATIONAL WATCHDOG**.
No exchange orders, POST transport, production Render/DB change or key modification.

## Files and evidence

- `app/t100_pilot_watchdog_observer.py` — existing GET-only per-snapshot
  assessment of one precommitted BTC_USDT short on an isolated 1x pilot account.
- `app/t100_pilot_watchdog_runtime.py` — new asyncio poll loop with a
  **local single-process filesystem lock**, SQLite `synchronous=FULL`,
  retained alert latch, and at-least-once notification outbox.
- `tests/test_t100_pilot_watchdog_runtime.py` — mock-based failure regression
  tests for restart, journal-bound scope, stale/lost notifications, timeout,
  invalid verdict, concurrent local instance and clock rollback.
- CI: both the full offline suite and the explicit pure-Python tests compile
  and execute this module. No live venue or credential is needed.

## Guaranteed by the implementation (within its trust assumptions)

1. The injected observer must return a redacted `PilotWatchVerdict` from
   the authenticated-GET-only observer. Unknown or structurally inconsistent
   success claims are converted to alarms; market state cannot authorize POST.
2. Each observation writes the latest redacted `WATCH_*` status and monotone
   nondecreasing journal timestamp in a synchronous SQLite transaction.
3. On an alarm transition, the code **commits a pending outbox row before**
   invoking the injected notification callback. A failed/timeout/exception
   callback leaves that row pending for a later retry, including after restart.
4. Previously latched alarms are NEVER cleared by one subsequent healthy
   observation. Repeated identical alarm observations do not make new rows.
   After a safe-state transition, a newly occurring alarm generates a new row.
5. Each local journal is bound to a fixed 64-digit SHA-256 scope fingerprint;
   reopening with a different scope fails. `flock` prevents a second local
   runner from simultaneously opening the same journal path.
6. Every public verdict has `entry_allowed=false`,
   `live_authorization=false`, `emergency_close_executed=false` and
   `mexc_orders_sent=false`. The module has no network or exchange order API.

## Important limitations and failures

- **Not a running process yet.** The `run()` loop is only started by an
  injected caller. No production adapter, independent deployment, process
  supervisor, heartbeat-checking peer or Discord delivery has been configured.
- **No emergency close.** The outbox delivers a redacted notification code,
  not an order permit. It cannot enforce a stop gap or close a position.
- **Single-host lock only.** Filesystem `flock` is not cross-host fencing.
  Do not share the SQLite journal between Render replicas or Mac/Render.
- **At-least-once delivery.** Crash between notification delivery and
  marking the outbox row delivered may cause duplicate notification. No
  exactly-once claim is made; notification callback success is not proof the
  operator actually read the alert.
- **Journal liveness failure.** If a process, machine or disk dies before
  alert creation or all delivery attempts, there is no automatic third-party
  supervisor in this prototype. A separate heartbeat/liveness sentinel is
  necessary. Disk full/corruption produces a fatal exception, not permission
  to continue. Do not use this code to claim 24/7 coverage.
- **Timing uncertainty.** GET snapshots are non-atomic and polling/HTTP
  latency may exceed the alert threshold. A deadline breach can be recognized
  only on an actual successful observation; network failure is escalated as
  UNKNOWN, not evidence of flatness. No maximum-loss bound follows.
- **Scope trust.** Callers must independently certify account fingerprint,
  exact entry origin, native-stop identity and the read-only transport
  boundaries. The runner itself accepts injected callbacks and does not
  authenticate market or account data.
- **No production authorization.** The standalone test account's observed
  stop lifecycle does not certify four-slot V40 CROSS or main-account trading.

## Next work before any real amendment/stop-trigger experiment

1. Independently reviewed signed-GET adapter and an actually runnable
   **read-only** pilot service using a dedicated read-only API scope.
2. Dedicated independent process supervision and a separate missing-heartbeat
   alert; prove redacted outbound notification to the intended Discord channel.
3. Crash/restart, lost-network, time-skew, disk-full and failed-notifier drills
   using the deployed exact code SHA, not solely mocked unit tests.
4. Separate emergency-close transport and durable idempotent reconciliation,
   with a pre-approved one-position loss budget and no duplicate order on
   unknown outcomes. This is independent of the read-only runtime.
5. Only after review/approval, one supervised stop-amendment experiment and
   a separately approved native-stop-trigger fill experiment.

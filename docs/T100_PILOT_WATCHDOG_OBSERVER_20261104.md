# T100 pilot — independent GET-only exposure observer (2026-11-04)

## Scope and crucial non-claims

`app/t100_pilot_watchdog_observer.py` is a stand-alone **decision-only**
observer for the dedicated one-contract BTC_USDT isolated-1x pilot. It does
not import the scanner/trader, carry an API key, perform HTTP directly,
persist state, schedule polls, trigger Discord, place emergency market closes,
manage Render or access production databases. It consumes only an injected
`ReadOnlyMexcProbe` (GET methods) and operator-supplied immutable pilot
scope. Neither `entry_allowed` nor `live_authorization` can ever be true.

**This is not an operational independent watchdog or a certified emergency
close.** No independent process, deployment, alarm delivery, deadline
enforcement, order submission or crash drill is claimed.

The real pilot is documented at
[`T100_REAL_MEXC_PILOT_EVIDENCE_20261104.md`](T100_REAL_MEXC_PILOT_EVIDENCE_20261104.md).
The observed first fill to stop *creation* interval of 1,000 ms does not
measure time to ACTIVE protection and cannot establish a guaranteed loss cap.

## Input requirements

- Immutable account SHA-256 fingerprint, exact pre-committed external entry
  order ID, known native stop ID (optional only to return an alert), reviewed
  maximum stop price and bounded exposure-alert threshold (1–15,000 ms).
- Two complete account-level GET inventory snapshots, counts and two copies
  each of the exact entry order and entry fill ledger; snapshotted inside a
  bounded request window. The read sequence is non-atomic.
- `ReadOnlyMexcProbe` GETs only:
  `position/open_positions`, `stoporder/open_orders`,
  `order/open_order_total_count`, `order/external/BTC_USDT/{origin}`,
  and `order/deal_details/{orderId}`. No endpoint capable of mutation is
  imported or called by the adapter.
- Exact one-contract filled short and uniquely identified fills with
  coherent quantities/side/order ID, to prevent treating unrelated manual
  positions as owned by the pilot.
- Current exact one-position account, isolated 1x short, no unrelated orders
  and exactly one known native stop, verified as whole-position MARKET SL,
  non-reversing, normal, correct position origin and price (using the common
  strict native-stop audit).

## Result interpretation

| Status | Meaning | Operational handling (not implemented) |
|---|---|---|
| `WATCH_NATIVE_STOP_CURRENTLY_OBSERVED_NONAUTHORIZING` | A full-volume active GET stop matched the owned position in current bookends | Continue independent observation; this does not certify continuous protection or stop fill |
| `WATCH_FLAT_INVENTORY_OBSERVED_NONAUTHORIZING` | Bookends found no position/stop and no other orders | Read historical close/stop records before concluding normal closure |
| `WATCH_UNPROTECTED_EXPOSURE_NOW` | Owned short position currently lacks a stop | Alert supervisor immediately; block new entries |
| `WATCH_UNPROTECTED_EXPOSURE_DEADLINE_BREACHED` | Same exposure continues past the configured advisory threshold | Escalate immediately; review supervised close under separately verified controls |
| `WATCH_STOP_*_UNVERIFIED`, `WATCH_NATIVE_STOP_SEMANTICS_UNVERIFIED` | Stop missing identity, quantity, native-market fields or current active state | Alarm, halt; never assume stop is protective |
| `WATCH_UNRELATED_ORDER_OR_COUNT_MISMATCH`, `WATCH_POSITION_OWNERSHIP_OR_MODE_UNVERIFIED` | Scope or account conflicts with observed inventory | Quarantine; never adopt unrelated or manual positions |
| `WATCH_BOOKEND_DRIFT_UNVERIFIED`, `WATCH_SIGNED_GET_UNAVAILABLE` | Non-atomic snapshot changed or read failed | Mark UNKNOWN; do not infer flatness or retry any POST |

Every verdict is redacted: no keys, order IDs, account IDs, position IDs or
private error response. The observer returns only status and boolean
nonauthorization fields; it cannot place a close.

## Next production requirements — still blocked

1. Separate independent process and account-isolated credential scope, with
   a durable, immutable pre-entry intent and watchdog liveness check that
   cannot be suppressed by a crashed main process.
2. Actual alarm delivery, operator notification and testing of delayed,
   failed, duplicated and reordered GET observations.
3. Independently reviewed emergency-close executor with explicit position
   binding, audited reduce-only semantics, position-size reconciliation,
   durable request IDs, no blind retry after unknown response and
   correct handling of partial fills/stop-trigger race.
4. Defined loss budget and acceptable uncovered deadline. A 1-second
   creation timestamp is not proof of such a deadline.
5. In a separate expressly approved real experiment, validate native stop
   amendment without loss of coverage and later stop execution on trigger.
6. Crash/restart/network-partition testing and evidence-based release review
   before any Render production deployment or cross-margin four-slot use.

The source and test modules are included in
`.github/workflows/t100-live-safety-offline.yml`. All current tests are
offline mocks; **no real exchange orders** are authorized by them.

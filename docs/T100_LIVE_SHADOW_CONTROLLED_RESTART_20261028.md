# T100 read-only Render shadow controlled restart / 24h protected soak

**State on 2026-10-28: DEPLOY NOT AUTHORIZED YET.** Existing worker
`mexc-t100-live-shadow` in Frankfurt remains on prior commit
`f1a70ee859cb8263893656aced584142cc2480c0` and has a latched
`READ_FAILURE` from earlier public contract metadata timeouts, plus
a later private REST continuity failure. That history is not erased
by later healthy manual tests.

## Evidence before considering restart

- Operator's GET-only public contract detail from **same Render Shell**:
  3/3 fast reads (522/448/423 ms), 1,184 contracts on each.
- Operator's signed private REST↔WS↔REST in **same Render Shell**:
  `REST_WS_REST_CONTINUITY_STABLE`, 1 position and 1 active TP/SL
  both before/after, critical position/stop equality true, one WS pong.
  The `unRealizedPnl` drift is a volatile market value, not critical drift.
- No lifecycle WebSocket events: payload semantics and correlation remain
  unverified. Account-wide completeness and exchange atomicity unverified.
- Old worker's warning logs include public `contract_specs` `ReadTimeout`
  at 01:12/01:36/02:06 UTC and a later private `VenueReadHalted` at
  09:35 UTC. Presently no supported causal attribution to exhaustion.
- Resource metrics: approximately 0.0025 vCPU of 0.5 allocation,
  60–100 MB of 512 MB; no obvious CPU/RAM pressure.

## Reviewed changes on feature branch (not deployed)

- Public contract-detail refresh: **at most one retry after 1s** for
  specifically allowed connection/read transport exception classes.
  No retry for malformed responses, HTTP error/status, exchange business
  logic errors, or any PRIVATE account read.
- Recovered retries emit warning logs; repeated failure still triggers
  the same `READ_FAILURE` kill latch. Original kill latch never has a
  reset method. A new process begins a separate diagnostic run.
- REST↔WS continuity exception now emits only a constant,
  allowlisted `failure_stage` (e.g. `POSITIONS_GET_FAILED` /
  `OPEN_STOPS_GET_FAILED`), never an exception string/URL/body.
- Successful cycles emit a safe **INFO** heartbeat with random per-process
  `run_id`, monotonically increasing `cycle_index`, status,
  position and TP/SL presence booleans, critical consistency,
  continuity status, kill latch and non-authorizing flags.
  No symbols, contract sizes, prices, volumes, balance, IDs, or secrets.
- Top-level cycle errors now log exception CLASS only, never traceback
  or exception value. Discord kill and notification policy unchanged.

## Operator authorization boundary (manual decision required)

**DO NOT DEPLOY merely because tests are green.** Redeployment restarts
the shadow and creates a fresh in-memory kill latch. This is permissible
only as an explicitly operator-approved **new read-only diagnostic run**
after preserving the prior failure evidence; it must not be represented
as clearing or disproving the old failure. Do not deploy live trader.

Before manually triggering deployment:
1. Snapshot/preserve prior Render warnings and Discord kill messages.
2. Independently check the existing open futures position has an active
   native protective stop in MEXC; a count of 1 TP/SL is not proof of
   stop direction, volume coverage or trigger price.
3. Verify from **Render worker's configuration**:
   `T100_EXECUTION_MODE=paper`,
   `T100_LIVE_ORDER_API_ENABLED=false`,
   `T100_LIVE_KILL_SWITCH=true`, and `autoDeploy=off`.
   Confirm `startCommand=python -m app.t100_live_shadow_runtime`.
   Do not print credentials or change the existing scanner/paper trader.
4. Review the exact branch SHA and all five offline CI outcomes.
5. Explicit operator approval for a one-off, **read-only shadow**
   deployment and brand-new observation window. No live-trading approval
   is requested or implied.

After operator-approved read-only deployment:
- Confirm Render deploy is `live` at the expected feature SHA, no
  extra instance, no order-capable code path.
- Confirm Discord startup and new `run_id` heartbeat entries.
- In first cycle, verify `OBSERVING_NON_AUTHORIZING`,
  `position_observed=True`, `tpsl_observed=True`,
  `critical_consistent=True`, `continuity_status` stable,
  `kill_tripped=False`; if not, stop treating it as a healthy soak.
- Do not interpret a transient retry as healthy zero-error behavior.
  Count every `contract_specs transport retried` warning separately.
- Track 24 consecutive hours of one run ID, increasing cycle indices,
  without uncovered gaps/restarts and without ANY read failure, kill
  warning, unexpected private WS channel, lost protection, or
  recovered metadata retry. Allow for ordinary time jitter but investigate
  gaps >180s (configured cycle cadence is 90s).
- If a **position closes naturally** or no native stop is observable,
  a protected-position soak ends even if worker continues and no kill.
- If any failure/restart occurs, historical attempt is marked FAILED/
  INCOMPLETE and subsequent fresh run is a *new* independent attempt.

Passing a protected observation soak remains **non-authorizing**.
It does not certify account-wide completeness, exchange atomicity,
stop amendment semantics, real order fills, strategy execution parity,
execution-cost gap model, leverage/liquidation behavior or independent
review. Do not turn on MEXC POST order or stop-mutation permissions.

# T100/V40: full autonomous four-slot productionization target

**Decision (2026-10-28):** Build directly for the complete autonomous T100/V40
system. The single-position tiny pilot is NOT the architecture or release target.
This decision is **not authorization to transmit exchange orders**.

## Frozen strategy contract — do not silently change
- Four independently owned positions/slots, one open position per symbol.
- Prior 49 completed consecutive 30-minute bars. Never include the entry bar.
- ATR candidate floor 4%; high tier begins at ATR7 >= 5.7%.
- Skip admission when prior-24h volatility >= 30%.
- LOW gross target 100% / 4 slots = **25% of equity per slot**.
- HIGH gross target 250% / 4 slots = **62.5% of equity per slot**, BEFORE
  liquidity, shock-budget, lot, account risk and margin clipping.
- Prior-turnover participation, H4 stressed exit capacity and same-bar quota
  are used exactly as in `app/t100_v40_admission.py`.
- **H4 = four-bar stressed EXIT capacity assumption, NOT permission for a
  120-minute staged ENTRY campaign.** Do not treat later fills as same-candle
  historical entry parity.
- Frozen V39 lot quantization; any exchange child-order execution is a
  new implementation requiring independent time/price/fill evidence.
- Use actual account exposure and point-in-time contract specs; never promote
  synthetic fills into exchange-realized P&L.

## Known incompatible prototypes that must not be promoted as V40
1. `app/t100_live_vol30.py` currently limits HIGH parent target to **56.25%**
   and implements a **120-minute H4 entry window**, which diverges from the
   frozen V40 policy (62.5% HIGH slot target and H4 exit-capacity horizon).
2. `app/t100_live_execution.py::LiveOrderGateway.send_one` rejects all
   accounts with an existing position: a one-position pilot constraint.
3. `app/t100_live_entry_recovery.py` demands exactly one post-entry position
   and stop: a one-position pilot constraint.
4. `app/t100_live_pilot_coordinator.py`, `pilot_sizing.py` and
   `pilot_readiness.py` are one-position pilot interfaces. Do not
   automatically adapt them into four-slot authorization.
5. `app/t100_live_guards.py` retains `LIVE_ADAPTER_STATUS = NOT_IMPLEMENTED`.
   At final POST boundaries, the adapter certification gate remains fail
   closed. Changing environment flags is not a substitute for certification.
6. The frozen portfolio can target 250% gross exposure; a 1x isolated
   account may not support simultaneous unclipped targets. **Never use
   leverage to claim a liquidity/slippage fix.** Real margin/capacity clipping
   must be recorded as an explicit strategy-vs-execution deviation.

## Full-system implementation sequence (no pilot shortcut)
1. Implement pinned full-V40 admission and same-candle decision parity over
   the complete chronological replay: slot occupancy, queue ordering,
   turnover/shock clipping, contract availability and point-in-time spec.
2. Implement a single leader-fenced multi-slot execution coordinator:
   T100-owned parent/child reservations, deterministic externalOid,
   atomic pre-POST journal transition, no POST retries on UNKNOWN,
   account-wide pre- and post-order reconciliation, no unrelated positions.
3. Generalize protected-entry fill/position/stop reconciliation from one
   position to 0–4 separately owned positions; keep full entry origin,
   immutable trade IDs, fee/funding records, stop linkage and exactly one
   current checkpoint/revision chain per managed position.
4. Implement MEXC-native stop create/update/close choreography with exchange
   GET verification, write-ahead amendments, timeout/unknown recovery,
   monotonic tightening, controlled close accounting and no uncovered stops.
5. Prove crash/restart correctness at every stage with deterministic fault
   injection, independent complete inventory, fencing takeover, exchange
   and database continuity. Never adopt unowned/manual MEXC positions.
6. Require actual live fill/slippage/market-impact observations plus
   capacity gating, emergency operator controls, Discord incident delivery,
   and independent review. Historical replay is not live-fill certification.
7. Complete a sustained uninterrupted protected shadow soak and full-system
   offline/shadow concurrency test. Request **separate explicit approval**
   before a full-system live release.

## Release invariants
- No MEXC POST, stop mutation or live-order permission change in this plan.
- If any of 4 slots is ambiguous, exposure or protection is not complete,
  ledger ownership is missing, or a signal would breach current caps, block
  new entries and escalate rather than silently synthesizing a fill.
- Realized fills, losses, fees, funding, equity and liquidation exposure must
  be computed from independently reconciled exchange evidence.
- $2,000 is the planned initial funding context, **not a promised return**.
- The current unrelated MEXC position must never be adopted into T100.
- Existing scanner/paper trader and read-only Render shadow remain separate.
- Deployments remain manual until reviewed; LIVE_ADAPTER_STATUS stays
  NOT_IMPLEMENTED until the actual full coordinator is independently certified.

## Implementation checkpoint: four-slot model gate (2026-10-28)

- `app/t100_live_four_slot_planner.py` now runs frozen, single-candle V40
  admission over a **read-only** representation of 0–4 account positions.
- A non-flat account must supply a structured `DurableRecoveryReport`, not
  a boolean, position count or guessed ownership. The model checks report
  cardinality and identity uniqueness and refuses unowned positions,
  unmatched native-stop counts, unverified inventory, duplicate symbols,
  unrelated inflight orders, and the fifth slot.
- This is **model planning only**, never a leader-fenced execution permit.
  It has no DB/REST calls, no atomic reservations and no MEXC POST.
- The runtime still lacks a binding of the **same verified snapshot window**
  to checkpoint/order/fill provenance. Passing counts alone is not a live
  proof of exact identity or absence of intervening fills. Live integration
  must refresh and re-reconcile under a fenced transaction and fail closed
  on drift.
- The next engineering implementation is a multi-position **read-only
  provenance/snapshot coordinator** with deterministic restart/fault
  injection, followed by durable, atomic four-slot reservations and
  single-attempt exchange submission. Existing one-slot pilot paths are
  not used as live release interfaces.

## Implementation checkpoint: four-slot read-only provenance bookend (2026-10-28)

- `app/t100_live_four_slot_snapshot_coordinator.py` consumes four ledger
  collections, exact GET-by-externalOid orders, complete account positions,
  current native TP/SL rows and an independently produced account inventory
  completeness report. It bookends the MEXC critical fields and the entire
  selected ledger state, blocking on drift, absent origins, unknown states,
  extra orders, unrelated positions/stops and duplicate identities. Successful
  1–4 position reconstruction uses `recover_nonflat` and remains explicitly
  NONAUTHORIZING; a count-only or manual MEXC position cannot be adopted.
- `app/t100_live_four_slot_ledger_reader.py` supplies the four ledger
  collections from one verified restricted-role, expected-database, read-only
  REPEATABLE READ PostgreSQL transaction. No SQL mutations are permitted.
- The coordinator is not yet wired to a runtime MEXC inventory completeness
  adapter or to the continuous Render supervisor, and does not establish a
  distributed atomic snapshot, fresh leader lease or live-fill certification.
  An external caller must not treat its return value as permission to POST.
- The deliberately strict flat-history rule blocks accounts with historical
  intents/amendments until a separate completed-position/closed-ledger audit
  is implemented. This is a known release blocker after the first full cycle,
  not a reason to delete historical records or relax provenance checks.
- Deterministic offline tests cover complete 3/4 slots, frozen V40 admission,
  read-only transaction/context enforcement, unowned existing positions,
  duplicate identities, missing order, stop-price and volume drift, DB change,
  unknown intent state, unrelated orders, incomplete and unstable inventory,
  and source outages.

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

## Implementation checkpoint: four-slot reservation journal (2026-10-28)

- Added **CREATE-ONLY, UNAPPLIED** `migrations/031_t100_live_v40_four_slot_reservations.sql`,
  separate from older VOL30-H4 entry-campaign parents/children. This file is
  **NOT** added to the pinned migration 027–030 CLI; production PostgreSQL has
  NOT been changed and there is no runtime writer for this new table yet.
- Added `app/t100_live_four_slot_reservations.py`:
  a cooperating PostgreSQL writer acquires the **account-wide advisory
  transaction lock first**, checks an unexpired matching fenced leader row,
  validates the expected database, limits witness age, rereads all four
  critical ledger collections, compares their digest, verifies every owned
  checkpoint identity, checks existing reservations + unresolved old ledger
  mutations, and performs exactly one `RESERVED` insert.
- The database partial unique index prevents duplicate active-symbol rows;
  `signal_id` is a primary key. The four-slot count is **cooperatively
  serialized via the global transaction lock**; PostgreSQL does not enforce
  the global count with a standalone CHECK. Any future insert/transition
  writer must follow the same lock and state machine.
- A RESERVATION is deliberately not an order intent, stop protection,
  execution permission, or exchange fill. This module has **no MEXC POST**,
  never grants live authorization, does not advance `SUBMITTING`, and does
  not release reservations. Unresolved states block global new entries.
- The account snapshot witness includes independent inventory and durable
  recovery evidence, but the supplied observation timestamp is still an
  input and its proximity to reservation time cannot establish exchange
  atomicity, absence of ABA changes, or safe multi-step execution.
- Local/CI concurrency simulations exercise contention for the fourth slot,
  six concurrent requests for four slots, duplicate symbols/signals, stale
  fencing, clock skew, ledger drift, unknown outcomes, legacy-parent state
  and DB error redaction. These simulations **do not replace real PostgreSQL
  rollback/lock and MEXC account-fence integration testing**.

### Next release blockers
1. Operator-approved migration plan, restricted write-role/GRANT audit,
   independently repeated database backup and tested rollback. Do not apply
   migration 031 merely because this code is green.
2. Live, independent, token-fenced exchange snapshot immediately before
   any POST. Bookended GETs are not an atomic venue lock.
3. Durable transition `RESERVED -> SUBMITTING` with an independently
   rechecked leader and account-wide risk state, exactly-once-attempt
   externalOid order POST, UNKNOWN outcome reconciliation and no retry.
4. 0–4 protected-entry checkpoint ownership after fill, crash-safe
   stop amendments/closures, completed-position audit, margin and capacity
   checks, full chronological strategy parity, long-running shadow
   readiness, and explicit operator authorization.

## Implementation checkpoint: atomic four-slot order-attempt journal (2026-10-28)

- `app/t100_live_four_slot_attempt_journal.py` implements the **DB-only**
  precursor to an exchange submission. Under the same fixed two-key account
  advisory transaction lock used by the V40 reservation store, it checks
  the exact fencing lease and reservation identity, the unchanged ledger
  digest and protected checkpoint identities, a two-second observation age,
  unresolved old intents/amendments and distinct occupied/reserved symbols.
- Entry intent volume, contract size, volume step, reference price and
  tick-quantized 75% short stop must be numerically coherent; actual order
  notional may only **reduce**, never exceed, the reserved V40 modeled
  notional. This alone does not independently verify current contract specs,
  real margin, liquidation risk or order-book liquidity.
- `PREPARED` intent insert, intent `SUBMITTING` CAS, and reservation
  `RESERVED -> SUBMITTING` CAS occur in **one PostgreSQL transaction**.
  If the DB operation fails, both are rolled back. Once committed,
  `SUBMITTING` must be treated as possibly posted, even though this
  isolated module has no MEXC POST transport. Ambiguous outcomes can
  atomically latch both rows `UNKNOWN`; no automatic retry exists.
- This attempt journal is **not wired to Render or MEXC**, does not issue
  mutation permits, and cannot mark `ACK`, `PROTECTED` or `RELEASED`.
  The new table remains UNAPPLIED. A certified transport coordinator
  must separately establish a fresh leader, account, order-book, and
  contract/risk snapshot immediately before a single POST.
- Offline tests use simulated PostgreSQL transaction rollback and
  contention; **real PostgreSQL isolation and crash behavior are not
  yet certified**, nor is an independent exchange order lookup.
- Next: implement **GET-only externalOid order outcome and fills
  reconciliation** with ledger CAS, unknown/missing outcomes left blocked,
  then protected-entry checkpoint persistence for 0–4 simultaneous
  positions and a fully controlled close/stop lifecycle.

## Implementation checkpoint: MEXC cross-margin model (2026-10-28)

MEXC primary documentation checked:
- [Liquidation FAQ](https://www.mexc.com/support/article/faq-on-liquidation-for-futures-trading-8123281969561)
  (2026-02-11): FAIR price triggers liquidation; cross-margin equity
  excludes isolated margin, isolated unrealized PNL and order margins;
  liquidation threshold includes maintenance margin **plus liquidation fee**;
  liquidation can cancel orders and proceed through partial risk-tier steps.
- [Private GET risk limits](https://www.mexc.co/api-docs/futures/account-and-trading-endpoints/get-risk-limits)
  (`/api/v1/private/account/risk_limit`): current side-specific risk tier,
  maintenance margin rate, initial margin rate, maximum position contracts,
  leverage restrictions. The CURRENT tier is not proof of the WORST tier
  under a future 20% price move.
- [Public contract metadata / WS](https://www.mexc.co/api-docs/futures/websocket-api/contract-data):
  liquidation fee rate, contractSize, support for cross/isolated mode,
  risk increments, contract availability and updated specs.

### New pure risk calculator
`app/t100_live_cross_margin_stress.py` computes a 0–4 slot
ALL-SHORT USDT cross-margin model with explicit wallet, current FAIR mark,
entry quantity/price, configured leverage, isolated and order margin,
future fee/cashflow reserves, liquidation fees, and an independently verified
**worst stressed-tier maintenance upper bound** for each symbol.

The model makes a *conditional* 20% simultaneous adverse FAIR-price
assessment and an initial-margin feasibility check. It does **NOT** silently
limit V40 gross targets to 100% equity; tests explicitly compare the
four-high-slot 250% gross / 5x modeled case with the infeasible 1x case.

The **fictional test inputs** are intentionally not MEXC risk-tier
certificates or observed account economic data. Scenario model survival
is not a live liquidation certificate, cannot prove stop fills, and never
authorizes an order.

### Explicit blockers to venue-specific certification
1. Collect/read-only verify the live account's cross-margin currency scope,
   wallet balance (excluding unrealized PNL), observed fair marks, open
   positions, committed order margin, isolates and fee/funding reserves;
   prove completeness and common observation window.
2. Resolve contract mode/position-side eligibility, **leverage and cap**
   per exact symbol. Confirm all stressed risk-tier upper bounds and
   liquidation fees, not a global 5% maintenance assumption.
3. Independently reconcile model calculation against MEXC's observed
   margin ratio and liquidation prices at multiple exposures, accounting
   for venue-specific discounts/tiers and dynamic mark prices.
4. Replay the full historical four-slot portfolio along real **fair-price
   paths**, including gaps, simultaneous funding, partial liquidation,
   unfilled protective stops, maintenance changes and ADL risks.
5. Do not replace the frozen research shock budget; use this model as a
   *separate safety overlay*. Quantify any difference in admission/size
   as live execution nonparity.
6. Live execution and Render services remain unchanged; migration 031 is
   unapplied and LIVE_ADAPTER_STATUS remains NOT_IMPLEMENTED.

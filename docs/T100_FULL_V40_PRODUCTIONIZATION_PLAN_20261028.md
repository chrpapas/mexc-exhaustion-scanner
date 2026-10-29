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

## Implementation checkpoint: current MEXC risk-tier witness (2026-10-28)

- Added `app/t100_live_risk_tier_witness.py` and offline drift tests.
  The pure witness compares the exact short side in the private GET
  `/api/v1/private/account/risk_limit` with a public GET
  `/api/v1/contract/detail/country` contract record for the SAME symbol.
- Source: [MEXC current risk limits](https://www.mexc.co/api-docs/futures/account-and-trading-endpoints/get-risk-limits)
  documents that `maxVol` may be converted to contracts using the CURRENT
  fair price when the limit is value-based. Therefore private `maxVol`
  is NOT taken to be a future-shock volume/capacity guarantee.
- Source: [MEXC contract metadata](https://www.mexc.co/api-docs/futures/market-endpoints/get-contract-info)
  documents `positionOpenType`, `riskLimitType`, `riskLimitMode`,
  `riskLimitCustom` level bands, `apiAllowed`,
  `countryConfigContractMaxLeverage`, and `liquidationFeeRate`.
- For this first version, only fully parsed, monotonically ordered public
  `CUSTOM`/`BY_VOLUME` static contract-quantity tiers are diagnosed.
  Missing/custom value-based (`BY_VALUE`) and incremental (`INCREASE`)
  schemes remain BLOCKED, not inferred.
- Checks exact short side, cross mode, account leverage, published symbol
  availability, quote/settle currency, contract size, restricted
  leverage, current risk MMR not below current public-tier MMR,
  distinct tier caps and ordered nondecreasing MMR/IMR.
- Conservatively reports the largest current published MMR (also
  including private current MMRS). It **NEVER** asserts
  `stressed_tier_upper_bound_verified`, `entry_allowed` or
  `live_authorization` because a current public schedule does not certify
  future emergency parameter changes, liquidation behavior, or the
  live account's actual clock-synchronized position/fair-price state.
- Existing `ReadOnlyMexcProbe.raw_account_risk_limits` is GET-only;
  this pure validation is not yet wired to it in any Render worker.
  No credentials, order mutation, leverage change, migration or deployment.
- Next is a privacy-safe read-only co-observation of risk-limit and
  public contract metadata, MEXC fair mark, full account collateral and
  maintenance-margin witness. Only after that evidence and historical
  path stress could the full V40 release gate be considered.

## Implementation checkpoint: double-read cross-risk observation (2026-10-28)

- Added `app/t100_live_cross_risk_observation.py`. It consumes **two
  separately injected GET-only frames**, each containing current positions,
  current stops, account assets, private current risk tiers, public contract
  metadata and timestamped fair prices for the SAME 1–4 symbols.
- Each frame checks complete-collection assertions (caller-supplied, not
  independently certified here), position/stop linkage and uniqueness,
  all-short/cross mode, matching private/public risk tier evidence,
  current symbol set, positive recent fair prices, and the existing
  position/USDT-asset margin+unrealized economic consistency witness.
- The read window must be consecutive and fresh; critical position/stop,
  public-contract, private-risk and cash/frozen collateral metadata must
  be stable across the window. Fair price may move within a bounded
  threshold, but an unstable fair mark or a stale market quote blocks.
  Full exchange atomicity and ABA-change absence are **not proven**.
- MEXC [Get All Account Assets](https://www.mexc.co/api-docs/futures/account-and-trading-endpoints/get-all-account-assets)
  describes `cashBalance` as withdrawable balance and `equity` as
  total equity. We must NOT silently treat `cashBalance` as the complete
  cross-margin wallet backing the V40 liquidation model.
  The reported economic witness only compares observed position margin
  and unrealized PNL with the returned asset data.
- The module reports ONLY redacted counts/booleans/a SHA256 fingerprint.
  No position IDs, account cash, symbols or order details are emitted.
  A successful observation is **CURRENT_CROSS_RISK_OBSERVED_NONAUTHORIZING**.
  `account_wide_completeness_certified`,
  `future_stressed_tiers_certified`,
  `venue_liquidation_certified`, `entry_allowed`
  and `live_authorization` are always FALSE.
- This is currently a **pure observation contract plus fake-reader tests**,
  not yet a deployed read-only private/public MEXC collector.
  Actual MEXC schema/timestamp validation, synchronized portfolio/asset
  reconciliation, ownership fencing, and cross-margin wallet construction
  remain future integration gates.
- The current unrelated MEXC position is never adopted by this module.
  No new migration, MEXC POST, leverage setting, Render deployment or
  production enablement occurred.

## Implementation checkpoint: GET-only risk frame collector (2026-10-28)

- `app/t100_live_cross_risk_collector.py` builds a genuine **transport-facing
  GET-only frame contract** from an injected `ReadOnlyMexcProbe` and a
  public HTTP GET client, with no credential handling or side-effect calls.
  It is **not** connected to a Render worker or executed against a personal
  live account yet; current tests use fake GET responses only.
- The collector performs account-wide positions/current TP-SL GET, redundant
  symbol-scoped GETs, all-assets GET, account-wide order counts, separate
  private risk-limit GET for each exact observed symbol, COUNTRY contract
  metadata GET, and dedicated **FAIR-price GET with exchange timestamp**.
  It ends with another account-wide positions/current TP-SL GET, checking
  critical-field continuity and the established current-inventory
  completeness model. Only supported 1–4 non-flat symbols are observed;
  this is *not* T100 ownership evidence.
- Primary sources:
  [GET risk limits](https://www.mexc.co/api-docs/futures/account-and-trading-endpoints/get-risk-limits),
  [GET country contract details](https://www.mexc.co/api-docs/futures/market-endpoints/get-contract-info),
  [GET fair price](https://www.mexc.co/api-docs/futures/market-endpoints/get-fair-price),
  [GET account assets](https://www.mexc.co/api-docs/futures/account-and-trading-endpoints/get-all-account-assets).
  Specifically, fair-price data includes a **system timestamp**;
  the ticker `timestamp` is a **trade timestamp**, not a substitute.
- Bound the full GET frame duration, reject stale/future fair marks, changed
  positions/stops, symbol/risk/contract response mismatches, inconsistent
  account counts, transport errors, and mismatched asset economics. No
  output contains symbols, IDs, balances or account secrets.
- The successful `CrossRiskRead` output can be consumed by the existing
  `observe_cross_risk_window` double-read witness, but does not set
  `account_wide_completeness_certified`,
  `future_stressed_tiers_certified`,
  `venue_liquidation_certified` or trading authorization. An actual
  end-to-end GET collection may fail the 2.5-second time bound; this must
  be measured with real latency and either explicitly retuned/reviewed
  or left blocked rather than silently claiming freshness.
- A complete cross-margin wallet/liquidation balance cannot be inferred
  from `cashBalance` (withdrawable), and MEXC historical partial-
  liquidation and dynamic risk-tier behavior remain unmodeled. Next
  is an opt-in, redacted **read-only** operator diagnostic with
  independently checked venue session/time, account ownership isolation,
  and complete collateral reconciliation. No authorization for MEXC POST.

## Implementation checkpoint: operator opt-in GET-only cross-risk diagnostic (2026-10-28)

- Added `app/t100_live_cross_risk_diagnostic.py` as a **manual-only,
  redacted CLI**. It does not launch a worker, write PostgreSQL, deploy
  Render, change leverage, send orders or call ANY POST method.
- Explicit guards require
  `T100_EXECUTION_MODE=paper`,
  `T100_LIVE_ORDER_API_ENABLED=false`, and
  `T100_LIVE_KILL_SWITCH=true` **before reading credentials or opening
  a network connection**. A separate read-only scoped API key is strongly
  preferred; never share or log MEXC keys.
- The command wraps the existing authenticated MEXC private-GET
  transport (from `MEXC_API_KEY`, `MEXC_API_SECRET` already configured
  locally) and HTTP public-GET transport from the same client. It then
  performs two bounded, read-only `CrossRiskRead` frames.
- The CLI returns only allowlisted machine-readable status flags and
  position count (0–4). It does NOT output raw payloads, MEXC symbols,
  position IDs, account/collateral amounts, prices, timestamps, query
  URLs, private GET failure details, or credentials.
- Success means only
  `CURRENT_CROSS_RISK_OBSERVED_NONAUTHORIZING`, not ownership,
  atomicity, validated cross-wallet collateral, future risk-tier
  certification, protected live stops, or permission to trade.
  Some real positions may correctly FAIL due to isolated margin,
  dynamic risk tiers, differing venue stop schemas, incomplete
  economic reconciliation or the deliberately strict GET-time budget.
- The CLI has not been executed against real MEXC credentials by CI.
  GitHub actions exercise injected fake readers and guard paths only.
  An operator may run the following locally, then share only the
  redacted JSON output:
  ```bash
  T100_EXECUTION_MODE=paper \
  T100_LIVE_ORDER_API_ENABLED=false \
  T100_LIVE_KILL_SWITCH=true \
  python3 -m app.t100_live_cross_risk_diagnostic
  ```
- No service deployment or MEXC mutation accompanied this addition.

## Implementation checkpoint: redacted read/validation failure diagnosis (2026-10-28)

- First real operator-run diagnostic returned
  `CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED`. The original tool collapsed
  read-transport and frame-validation errors into one broad status.
  `observed_positions=0` was a default, NOT verified account flatness.
- Updated `app/t100_live_cross_risk_observation.py` to retain the previous
  fail-closed status while exposing FIXED allowlisted `failure_stage`
  (`READ_FIRST`, `VALIDATE_FIRST`, `READ_SECOND`,
  `VALIDATE_SECOND`) and `failure_code`.
  `app/t100_live_cross_risk_collector.py` classifies previously unknown
  transport/schema failures by a hard-coded GET phase, never by exchange
  payload or URL.
- The CLI `app/t100_live_cross_risk_diagnostic.py` emits these fixed
  redacted codes only and reports `observed_positions=null` on failure,
  instead of asserting that no positions exist. Unallowlisted exception
  messages are discarded rather than printed.
- Current MEXC risk-tier witness statuses (cross vs isolated, unsupported
  `BY_VALUE`/`INCREASE` tiers, unavailable public metadata, leverage
  mismatch, etc.) are mapped only through a static safe-status allowlist.
- `cashBalance=0` and `availableBalance=0` are valid observations
  in the non-authorizing READ witness; positive equity remains required.
  This is **not** a collateral adequacy check and never bypasses the
  V40 live risk gate.
- Existing unrelated account position remains completely unowned by the
  trader. The user must rerun the same GET-only CLI after pulling this
  commit and provide only `status`, `failure_stage`, `failure_code`
  and Boolean output. Never paste credentials or raw account details.
  No Render deployment, migrations or exchange mutations.

## Implementation checkpoint: flat/nonflat inventory-shape diagnostic (2026-10-29)

- An operator's real read-only risk run returned
  `failure_stage=READ_FIRST`,
  `failure_code=COLLECTOR_NONFLAT_ONE_TO_FOUR_REQUIRED`.
  The initial collector rejects any input NOT in 1–4 positions with
  exactly the same number of current TP/SL rows. This status alone cannot
  establish flatness, stop protectiveness, or liquidation safety.
- Added `app/t100_live_cross_risk_inventory_shape.py`: a **separate
  GET-only two-read shape observer** that rereads account-wide positions,
  current stops and account-wide open-order counts twice and compares
  safety-critical inventory fields and count identity. It reports only
  redacted counts and fixed diagnostic statuses:
  flat-and-order-free observed nonatomically, flat-with-open-orders,
  stops-without-position, more-than-four-positions, current TP/SL row
  count different from number of positions, order-count mismatch, or
  drift/transport failure.
- The manual `cross_risk_diagnostic` now invokes that observer **only**
  after `COLLECTOR_NONFLAT_ONE_TO_FOUR_REQUIRED`, and returns
  `inventory_shape_status`, `inventory_shape_stable`,
  `observed_current_stops`, `reported_stop_orders`, and
  `reported_total_open_orders`. It may report zero positions only
  when the independent double-GET is stable; otherwise count stays null.
- This does NOT loosen the risk model to assume zero margin requirements
  in a flat account. A genuinely flat account has no position to assess
  for liquidation; the planned hypothetical V40 1–4 slot candidate
  portfolio must be evaluated independently with verified actual contract
  tiers, cross-wallet economics, and stress prices.
- A TP/SL row count different from the position count does not prove
  missing protection: MEXC may allow multiple protective orders per
  position. Native stop semantics, linkage and volume coverage need
  separate evaluation. Existing manual/unowned MEXC positions remain
  untouched and never become T100-owned by this diagnostic.
- No Render deployment, DB migration, leverage change, or MEXC mutation.
  The code continues to require paper mode, order API disabled, and
  an engaged kill switch. Local rerun emits redacted inventory status
  for follow-up.

## Implementation checkpoint: account observation independent of manual SL/TP (2026-10-29)

- Operator confirmed they personally removed their manual position's SL/TP.
  The read-only inventory snapshot independently matched exactly one
  open position, zero current TP/SL orders, zero other open orders.
  This did **not** constitute a T100 order or exchange-native protection.
- The cross-risk collector no longer assumes that the number of current
  TP/SL rows equals the number of open positions. A single manually held
  short with zero stops remains eligible for **account observation**
  (and independent margin/contract/risk-tier checks), provided actual
  inventory count completeness, symbol crosschecks, current account margin
  arithmetic, timing, short/cross mode, and MEXC per-symbol metadata
  evidence pass.
- The cross-risk two-frame witness now accepts zero or multiple current
  stop rows, compares their critical fields independently for bookend
  drift, and returns `observed_current_stops` plus
  `native_stop_protection_verified=False`. It does NOT infer stop
  protectiveness from order counts or the mere presence of a TP/SL row.
- The manual diagnostic likewise exposes the actual observed stop count
  on successful risk observation (instead of returning null), with
  explicit `native_stop_protection_verified=False` and
  `entry_allowed=False`, `live_authorization=False`.
- **No change** to T100-owned position recovery or order authorization:
  `FourSlotSnapshotCoordinator.inspect()` still checks matched position,
  checkpoint, ACK order and exact native stop, and fail-closes on an
  owned position with zero stops (`POSITION_STOP_COUNT_MISMATCH`).
  Added independent regression covering stable absence of ALL owned stops
  in both exchange reads and the inventory count witness. Do not use the
  account-risk observer as a replacement for durable protected recovery.
- One manual unprotected position may occupy real portfolio margin and
  risk. It is **not** safe to permit new T100 entries, and the exchange
  account must remain isolated or the unrelated position must be
  independently accounted for. This diagnostic does not alter user
  positions, post orders, apply migrations, or deploy to Render.
- The collector's current modeled scenario supports 1–4 cross-margin
  USDT-settled SHORT positions and strict `CUSTOM/BY_VOLUME` static tiers.
  If the operator's manual trade is isolated or the exchange publishes
  a different tier scheme, risk observation may still fail at the next
  relevant evidence gate, with a fixed redacted reason. That is a
  *separate* model limitation, not an SL/TP requirement.

## Implementation checkpoint: coherent post-position economic witness (2026-10-29)

- Operator's first real read-only risk frame advanced past the manual
  zero-stop count, then returned
  `failure_stage=VALIDATE_FIRST`,
  `failure_code=MARGIN_ECONOMIC_WITNESS_MISMATCH`.
  This means the returned account asset figures did not reconcile with the
  positions returned in that **first frame**. It does not establish whether
  the cause is MTM timing, exchange accounting semantics or a true mismatch.
- Found a deterministic code-level issue: the collector reconciled
  BEFORE positions against BEFORE assets and then returned AFTER positions
  against the stale BEFORE assets. The critical-field inventory hash
  intentionally excluded `unRealizedPnl`; natural fair-price movement
  could thus change the returned position PnL without changing the
  asset-side PnL. The frame validator then rightfully rejected it.
- The GET-only collector now separately reads AFTER assets after AFTER
  positions/stops and requires **both PRE position/assets and POST
  position/assets** to pass the existing exact economic witness. The
  returned `CrossRiskRead` carries AFTER positions and AFTER assets
  together; a PnL update without a matching account-asset update still
  blocks. No new tolerance, time relaxation or assumed wallet semantics.
- Added strictly allowlisted `COLLECTOR_PRE_*` and
  `COLLECTOR_POST_*` reason codes for numerical position-margin mismatch,
  unrealized-PnL mismatch, other-asset exposure, or invalid economic
  schema. Values, symbols, URLs and account identifiers remain absent
  from the CLI output.
- Tests independently demonstrate: natural PnL updates across the frame
  with matching new assets pass the collector; stale/failing assets,
  unmatched unrealized, changed position margin, and pre-pair mismatch
  fail closed with fixed codes and no POST.
- Exchange endpoints are still non-atomic, and even two coherent pairs
  cannot verify a venue liquidation engine. A real-account rerun is
  required to determine whether this resolved the observed failure.
  T100 ownership/native-stop recovery, available collateral, future
  risk tiers, and live authorization remain unverified and disabled.
- No MEXC orders, leverage changes, Render deployments or DB migrations.

## Implementation checkpoint: first private positions GET access diagnostics (2026-10-29)

- After manually restoring local MEXC API credentials, operator's read-only
  collector stopped at `READ_FIRST` with
  `COLLECTOR_READ_FAILED_AT_PRIVATE_POSITIONS`. This is an authenticated
  GET/read issue, NOT evidence of missing holdings, margin failure, or
  T100 trading readiness. Prior account evidence is not current proof.
- Added `app/t100_live_private_positions_access.py` and
  `app/t100_live_private_positions_access_cli.py`: a separate,
  explicit paper-mode + engaged-kill-switch **single-request signed GET**
  to `/api/v1/private/position/open_positions`. It reuses exactly the
  existing signing logic but inspects exchange error-code classifications
  without raising or serializing raw response messages. No HTTP POST,
  retry, portfolio action or Render deployment.
- It distinguishes allowlisted MEXC futures access errors: key expired
  (402), IP not in whitelist (406), endpoint permission denied (511),
  signed request timestamp/invalid request (513), signature rejected
  (602), account read required (701), trade-info read required (703),
  rate-limited (510), and others. Malformed/unknown responses, HTTP
  rejection, connectivity and timeout have safe generic statuses.
  HTTP status is reported only for a small fixed allowlist.
- MEXC's documented `open_positions` endpoint requires
  **View Order Details** permission. Source:
  https://www.mexc.co/api-docs/futures/account-and-trading-endpoints/get-open-positions
  Futures error codes: https://www.mexc.io/api-docs/futures/error-code
- Run locally with the already exported credentials (do not paste them):
  ```bash
  T100_EXECUTION_MODE=paper \
  T100_LIVE_ORDER_API_ENABLED=false \
  T100_LIVE_KILL_SWITCH=true \
  python3 -m app.t100_live_private_positions_access_cli
  ```
- Only return the redacted status and HTTP status. If the GET succeeds,
  rerun the original risk diagnostic. If it fails, fix the specific
  credential scope, IP, timestamp or transport issue locally; never
  turn on order permissions merely to make a read-only endpoint pass.
  Any existing stopless manual position remains independent of T100.
- No trading authorization, collateral certification, database migration,
  Render deployment, leverage changes or MEXC orders.

## Implementation checkpoint: MEXC risk-limit schedule classification (2026-10-29)

- Using an active MEXC API key, the operator's single private positions
  GET returned `POSITIONS_READ_SUCCEEDED` (HTTP 200). The full read-only
  cross-risk frame then failed at `VALIDATE_FIRST` with
  `RISK_TIER_STATIC_VOLUME_SCHEDULE_UNAVAILABLE`.
- This is a deliberate unsupported-contract-model gate, **not** proof
  that the exchange rejected the account or that liquidation occurred.
  `assess_risk_tier_witness` accepts only `CUSTOM + BY_VOLUME`
  schedules with a fully reviewed `riskLimitCustom` tier sequence.
  The failure can mean `INCREASE` mode, `BY_VALUE` type, or both;
  the original error did not distinguish them.
- MEXC contract-info documentation currently lists:
  `riskLimitMode` (`CUSTOM`, `INCREASE`),
  `riskLimitType` (`BY_VOLUME`, `BY_VALUE`),
  `riskLimitCustom` tiers and the incremental fields
  `riskBaseVol`, `riskIncrVol`, `riskIncrMmr`, `riskIncrImr`,
  `riskLevelLimit`, `maintenanceMarginRate`, `initialMarginRate`.
  Source: https://www.mexc.fm/api-docs/futures/market-endpoints/get-contract-info
  Current private `maxVol` may be converted from position value using
  current fair price; do not extrapolate it as a static contract cap.
  Source: https://www.mexc.co/api-docs/futures/account-and-trading-endpoints/get-risk-limits
- Added `app/t100_live_risk_schedule_shape.py`, a **pure, read-only
  descriptive classifier** of the public contract metadata already
  fetched in the first frame. It neither performs extra network requests
  nor uses public metadata to simulate future maintenance margin.
- On precisely `RISK_TIER_STATIC_VOLUME_SCHEDULE_UNAVAILABLE`, the
  `CrossRiskObservation` and manual CLI now expose fixed allowlisted
  `risk_schedule_mode`, `risk_schedule_type` and
  `risk_schedule_fields`. All symbols, quantities, prices, raw rates,
  account details, and raw exception data remain withheld. Because the
  first frame failed validation, the CLI always returns
  `risk_schedule_two_frame_certified=false`.
- The incremental field-presence check deliberately treats MEXC's
  documented single-tier INCREASE examples with nonzero increment fields
  as valid *metadata presence* without interpreting those as usable
  future tiers. Missing, malformed, or conflicting incremental fields
  are distinct diagnostics, not invitations to estimate.
- Existing risk-tier witness, four-slot native-stop recovery, auth gates,
  account ownership and live trading remain unchanged and fail closed.
  Never model INCREASE or BY_VALUE schedules using CUSTOM/BY_VOLUME
  assumptions; any future model must be independently reviewed and
  compared against current private tiers and stressed fair prices.
- Tests added for INCREASE/BY_VOLUME, CUSTOM/BY_VALUE, mixed/unknown
  contract metadata, lack of static schedule, and output injection.
  No DB migration, Render deployment, MEXC POST, order, or leverage changes.

## Implementation checkpoint: moving versus persistent unrealized PnL mismatch (2026-10-29)

- With the verified active MEXC key, the operator's GET-only cross-risk
  collector most recently returned
  `failure_stage=READ_FIRST`,
  `failure_code=COLLECTOR_POST_UNREALIZED_PNL_MISMATCH`.
  The positions, stops, assets, private risk and public contract reads ran,
  but the POST positions' `unRealizedPnl` did not exactly reconcile with
  POST account-assets `unrealized`. The PRE account witness had passed.
- This discrepancy could be asynchronous mark-to-market updates between
  independent REST endpoints. Alternatively, MEXC could report these fields
  on a different account/margin scope or valuation basis. The code MUST
  not assume either explanation. MEXC docs describe position `unRealizedPnl`
  and account `unrealized` but do NOT provide a single atomic REST snapshot:
  https://www.mexc.co/api-docs/futures/account-and-trading-endpoints/get-open-positions
  https://mexcdevelop.github.io/apidocs/contract_v1_en/
  MEXC also documents single-asset versus multi-asset cross-margin modes:
  https://www.mexc.co/support/article/how-to-trade-futures-on-mexc-web-305423980641926144
- Added `app/t100_live_pnl_sync_observation.py`, a separate limited diagnostic.
  It executes up to 3 sequential bounded GET triplets:
  **account-wide positions → all account assets → account-wide positions**,
  with a strict 2500ms per-triplet budget and no retries. It validates the
  safety-critical position fields and compares per-position PnL by immutable
  position identity, then uses the existing exact economic witness against
  the same intervening account-assets data. It reports only count summaries
  and static statuses, never amounts, symbols, prices or credentials.
- Distinct statuses:
  `PNL_SYNC_MOVING_MARK_TO_MARKET_OBSERVED` when position PnL changed
  during a triplet and stable disagreement was not observed;
  `PNL_SYNC_STABLE_UNREALIZED_DISAGREEMENT` when the position PnL was
  unchanged across a triplet but the intervening asset unrealized still
  differed; `PNL_SYNC_POSITION_MARGIN_DISAGREEMENT` for account-position
  margin mismatch; and fail-closed transport/schema/inventory-drift cases.
  Individual sample matches remain explicitly **NONATOMIC**.
- `app/t100_live_pnl_sync_cli.py` requires paper execution, disabled
  order API and engaged kill switch BEFORE importing the network client.
  It takes credentials only from local environment; it does not persist
  credentials or emit sensitive data. At most nine private GET requests
  are made when the operator opts in to a run.
- Crucially, the strict V40 cross-risk economic reconciliation is
  **unchanged**. This diagnostic only identifies why exact parity might
  fail and does not raise tolerances, bypass account-wide completeness,
  promote static tiers, or certify stress liquidation / account collateral.
  T100 four-slot native stop and durable ownership gates also remain
  fail-closed. If stable disagreement persists, inspect source account
  mode, field semantics, and mark valuation with MEXC before any redesign.
- Offline regression includes stable matched triplets, moving-PnL
  triplets, stable mismatches, position-margin mismatches, critical
  position drift, malformed PnL response, transport failure, cap on GET
  volume, and mandatory paper/kill preflight.
- No live orders, no leverage change, no database migration, no Render deploy.

## Implementation checkpoint: PnL-independent public risk schedule probe (2026-10-29)

- The operator's bounded read-only PnL triplet sampler returned
  `PNL_SYNC_STABLE_SAMPLES_RECONCILED_NONATOMIC`: three stable windows,
  three reconciled USDT position/asset unrealized PnL comparisons,
  zero position-margin mismatches, zero observed non-USDT exposure windows,
  and no critical position drift. This is encouraging but does not
  retrospectively invalidate the earlier first-frame
  `COLLECTOR_POST_UNREALIZED_PNL_MISMATCH`. It does not establish that
  all non-atomic REST snapshots will reconcile in future.
- Earlier risk witness also reported
  `RISK_TIER_STATIC_VOLUME_SCHEDULE_UNAVAILABLE`: the strict risk model
  only accepts public `CUSTOM + BY_VOLUME` fully static tiers. The risk
  mode/type classifier included in that witness cannot emit evidence when
  an independent PnL timing fault blocks validation first.
- Added `app/t100_live_risk_schedule_probe.py` and manual
  `app/t100_live_risk_schedule_probe_cli.py`. The standalone inspect
  does NOT call any account assets endpoint, allowing us to distinguish
  venue risk-mode support without loosening PnL parity. It performs
  exactly one account-wide private positions GET, at most four public
  country-contract-info GETs, and one account-wide positions bookend GET.
  It requires 1-4 distinct valid USDT contract symbols and checks stable
  critical position bookends. The public responses must confirm the
  requested symbol and success envelope.
- Only redacted public shape categories are emitted:
  `risk_schedule_mode` (`CUSTOM`, `INCREASE`, `UNKNOWN`, `MIXED`),
  `risk_schedule_type` (`BY_VOLUME`, `BY_VALUE`, `UNKNOWN`,
  `MIXED`), and `risk_schedule_fields` indicating present, absent,
  inconsistent or mixed metadata. No raw position identifiers, symbols,
  quantities, price, leverage, balance, or exchange message.
- The inspection is a metadata snapshot, not two independently matched
  public-contract frames, account-wide inventory completeness certification,
  verified private risk levels, future-tier protection, atomicity, collateral,
  risk reserve, liquidation model or a T100 execution permit.
  `entry_allowed=false` and `live_authorization=false` always.
- Paper execution + false live API + engaged kill switch are required
  BEFORE constructing any MEXC network client. No retries, POST calls,
  order placement, leverage modification, stop modification, Render deploy
  or DB mutations.
- Offline regressions cover CUSTOM/BY_VOLUME, INCREASE/BY_VALUE,
  malformed public data, critical positions changing during read,
  GET failures, empty positions, strict runtime guards and no leak.
- Next: run this once on the Mac with active *matching* read credentials,
  inspect the published risk-limit mode/type, then create a separate
  independently tested model for the *documented* scheme before relaxing
  no execution gates.

## Implementation checkpoint: INCREASE/BY_VOLUME candidate reconstruction against private current MMR (2026-10-29)

- The operator's independent GET-only public-contract shape probe reports:
  `status=RISK_SHAPE_PUBLIC_METADATA_OBSERVED_NONAUTHORIZING`;
  `observed_positions=1`; critical position bookends consistent;
  `risk_schedule_mode=INCREASE`, `risk_schedule_type=BY_VOLUME`,
  `risk_schedule_fields=INCREMENTAL_FIELDS_PRESENT_UNMODELED`.
  No current private tier, future liquidation schedule, account collateral,
  T100 ownership, native stops or live authorization was certified.
- Current MEXC official contract-info documentation defines
  `riskBaseVol`, `riskIncrVol`, `riskIncrMmr`, `riskIncrImr`,
  `riskLevelLimit`, `maintenanceMarginRate`,
  `initialMarginRate`, `riskLongShortSwitch` and `BY_VOLUME`:
  https://www.mexc.fm/api-docs/futures/market-endpoints/get-contract-info
  MEXC private GET risk-limit docs describe CURRENT `level` as
  min(leverage risk level, position risk level) and separately return
  `maxVol`, `mmr`, `imr`, `currentMmr`, `openType`, and leverage:
  https://www.mexc.co/api-docs/futures/account-and-trading-endpoints/get-risk-limits
- Added isolated *candidate* pure policy in
  `app/t100_live_incremental_tier_candidate.py`, strictly limited to
  `INCREASE + BY_VOLUME`. Using Decimal, the model hypothesizes
  `cap(level)=baseVol+(level-1)*incrVol` and
  `mmr(level)=maintenanceMarginRate+(level-1)*riskIncrMmr`,
  likewise IMR. Tier thresholds and upper bound are explicitly
  RESEARCH CANDIDATES inferred from fields named "per tier" and are not
  an exchange-verified implementation or future liquidation certificate.
- The candidate checks positive contract size, valid USDT perpetual,
  available/cross-eligible contract, all numeric fields finite, bounded
  level count, monotonically increasing contract quantity caps, valid
  MMR/IMR ordering and 0<=fee<1. When `riskLongShortSwitch=1`,
  short-specific base/increment must be present; **never** silently use
  generic or long-side limits. Missing/invalid metadata blocks.
- The CURRENT private short side may differ from volume-derived tier:
  published docs say `level` is affected by leverage-risk level too.
  The model compares **private current level's** `mmr` and optional
  `currentMmr` to the candidate for that exact level and returns
  `EXACT_MATCH`, `ABOVE_CANDIDATE`, or `BELOW_CANDIDATE` as
  *descriptive comparison only*. No equality to volume-inferred level is
  imposed and no matching private row certifies the full future schedule.
- Added opt-in `app/t100_live_incremental_tier_probe_cli.py` using
  `app/t100_live_incremental_tier_probe.py`, doing 2 account-wide
  position GETs, at most 4 private risk-limit GETs and 4 public
  country-contract GETs (max 10 GETs total), with no retries or order
  mutations. PnL/asset reads are independent and not bypassed in the
  unchanged actual entry/liquidation safety gates.
  It validates critical position bookends before reporting redacted,
  aggregate current private-versus-candidate MMR comparison counts.
- The standalone comparison always reports:
  `public_private_snapshot_atomic=false`,
  `future_stressed_tiers_certified=false`,
  `cross_wallet_collateral_certified=false`,
  `venue_liquidation_certified=false`,
  `account_wide_completeness_certified=false`,
  `native_stop_protection_verified=false`,
  `t100_position_ownership_certified=false`,
  `entry_allowed=false`, `live_authorization=false`.
  It does not emit symbols, position IDs, quantities, prices, account
  balances, raw tier rates, raw messages, keys or secrets.
- This code is not wired into the actual T100 four-slot planner,
  admission policy, cross-risk witness, Render runtime, or order path.
  Matching a currently observed tier is only one of several required
  independent confirmations for stress/liquidation modeling.
- Offline tests include tier boundaries, single-level zero increments,
  missing short-side fields, invalid/rate-incoherent tiers, private
  same/greater/less MMR, unknown/ineligible contract, private isolated
  mode, correct GET counts, bookend drift, response redaction, transport
  failure and mandatory paper/kill guards.
- No MEXC POST, stop/order change, Render deploy, DB migration or live
  authorization.

## Implementation checkpoint: public contract eligibility reason isolation (2026-10-29)

- After the first account snapshot classified the live manual contract
  `INCREASE + BY_VOLUME`, the standalone GET-only incremental tier
  candidate comparison halted with
  `candidate_failure=INCR_CONTRACT_NOT_ELIGIBLE`.
  This failure is **pre-MMR-comparison**. It does not prove that the
  contract is delisted, nor that the proposed incremental formula is
  correct. The public contract may be currently active but
  `apiAllowed=false`, isolated-only, nonperpetual, non-USDT, or
  missing a required field.
- Official MEXC country-contract GET documents separate
  `state` (0 enabled, 1 delivery, 2 delivered, 3 offline, 4 paused),
  `apiAllowed` (**API trading allowed**, a separate boolean),
  `positionOpenType` (1 isolated, 2 cross, 3 both),
  `futureType` (1 perpetual), `quoteCoin`, `settleCoin`:
  https://www.mexc.fm/api-docs/futures/market-endpoints/get-contract-info
  A private GET of an existing position does not imply permission to
  open an API position in its contract.
- Added `classify_incremental_eligibility` in
  `app/t100_live_incremental_tier_candidate.py`. This is a pure
  classifier returning only fixed public metadata categories:
  `contract_state`: ENABLED / NOT_ENABLED / UNKNOWN;
  `contract_api_order_access`: ALLOWED / DENIED / UNKNOWN;
  `contract_cross_margin_support`: CROSS_SUPPORTED / ISOLATED_ONLY / UNKNOWN;
  `contract_duration`: PERPETUAL / NOT_PERPETUAL / UNKNOWN;
  `contract_quote_currency` and `contract_settlement_currency`:
  USDT / OTHER / UNKNOWN.
- The existing numeric candidate eligibility gate is **not loosened**;
  it now delegates to the exact same allowlisted checks with strict
  integer and boolean types. The opt-in standalone
  `app.t100_live_incremental_tier_probe_cli` returns fixed
  categorical eligibility fields **only** for the particular
  `INCR_CONTRACT_NOT_ELIGIBLE` failure and only after stable
  position bookends. No additional MEXC GETs, API mutations or
  credentials are added, and the output does not include symbol,
  account ID, position size, prices, margins or raw exchange messages.
- If `contract_api_order_access=DENIED`, **do not** enable the T100
  trader on the contract even if the manual position is live. Manual
  position ownership/stop protection remains independent and unverified.
- The candidate current-private-MMR comparison remains unavailable
  until supported field semantics pass; a candidate match alone
  never certifies future stressed tiers or venue liquidation risk.
  Nothing is wired into V40 admission, current recovery, Render,
  position ownership or any order path.
- Tests cover each public eligibility dimension, missing/unknown
  fields, actual CLI results for apiAllowed false, isolated-only
  and missing apiAllowed, and no risk/trade approval in all cases.
- All live-order API, order placement, leverage/stop changes, Render
  deployment and migrations remain disabled.

## Implementation checkpoint: API-denied contract, independent tier research only (2026-10-29)

- The operator's active-account GET-only MEXC country contract probe
  returned `candidate_failure=INCR_CONTRACT_NOT_ELIGIBLE` with
  `contract_api_order_access=DENIED`, despite
  `contract_state=ENABLED`, `contract_cross_margin_support=CROSS_SUPPORTED`,
  `contract_duration=PERPETUAL`, and USDT quote/settlement.
  This is a **public per-contract API trading restriction**. It is
  NOT a failed API credential scope, and it is NOT proof that the market
  or an existing manual position is inactive.
- Official contract metadata documents `apiAllowed` as "Whether API
  trading is allowed"; `state=0` means the contract is enabled.
  https://www.mexc.fm/api-docs/futures/market-endpoints/get-contract-info
  The public contract's `apiAllowed=false` must ALWAYS block T100 API
  entry for that exact contract (and its status may change later).
  Current `app/t100_live_symbol_eligibility.py` independently verifies
  `api_allowed is True`; keep that guard. The observed manual position
  remains outside T100 ownership and native-stop authorization.
- The previous single `reconstruct_incremental_candidate` function
  also required `apiAllowed=true` before ANY tier arithmetic, so the
  unrelated numerical research was blocked. Separated that concern:
  `reconstruct_incremental_candidate` keeps its strict execution-eligible
  public contract requirement; a distinctly named
  `reconstruct_incremental_research_candidate` can examine an otherwise
  healthy public INCREASE/BY_VOLUME contract with **explicit
  `apiAllowed=false`**. It never lies to the calculation by rewriting
  `apiAllowed`, and it rejects missing/unknown `apiAllowed` along
  with non-enabled, non-cross, non-perpetual, non-USDT contracts.
- The standalone bounded GET-only
  `app.t100_live_incremental_tier_probe_cli` uses the research-only
  reconstruction; on matching critical position bookends it compares
  the published candidate against CURRENT signed private short-tier MMR.
  The CLI emits `contract_api_order_access=DENIED`,
  `api_trading_denied_contract_count`, and an explicit
  `INCR_PROBE_API_TRADING_DENIED_RISK_RESEARCH_ONLY` status if the
  candidate succeeds for an API-denied symbol. No private rates,
  symbols, balances, position ID, quantities, raw responses or credentials.
- This research result is NEVER wired into a mutation path or live-risk
  gate; even exact MMR match has zero standing for API execution,
  account-wide completeness, stressed tier safety, liquidation modeling
  or native-stop protection. All `entry_allowed=false` and
  `live_authorization=false` fields remain hardcoded.
- Additional offline tests assert the original strict API denial gate,
  research-only reconstruction on explicit denial, rejection of unknown
  permission or other ineligibility fields, correct private MMR comparison,
  and explicit CLI execution denial with no authorization.
- No REST POST, orders, stop change, leverage change, account mutation,
  Render deployment, DB migration or promotion.

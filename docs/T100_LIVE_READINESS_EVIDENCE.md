# T100 live readiness: evidence and release gates

**Status: BLOCKED — draft branch only. No Render deployment, no real orders.**

## Verified offline evidence
- V40 exported historical parity: 146 candidates; 2,376 gate rows,
  7,560 entry diagnostic comparisons, 648 exact REFERENCE lot notionals.
  These checks do not certify HYBRID/KNOWN_ONLY lot snapshots or a full
  independent equity/exit/funding replay.
- Dedicated offline CI covers admission, short trailing, read-only order
  lifecycle, stop journal, restart recovery, FIFO short accounting,
  strict timestamp/pagination inventory snapshot validation, and
  rejection of unexpected native stop orders.
- CI is an offline correctness check; it is not a MEXC API certification.

## Release blockers (all mandatory)
1. **Venue contract:** pin official MEXC API specification and verify every
   private read field against actual account responses in read-only mode.
   Cover order state codes, position IDs, side/hedge mode, stop plan IDs,
   contract sizes, lot rounding, timestamps and pagination.
2. **Historical realism:** independently reproduce portfolio exposure,
   execution, stop/trail events, fees/funding, and adverse gap behavior
   using point-in-time contract specs. Backtest modeled equity is not
   exchange-realized equity.
3. **Trade completeness:** build a paginated, restart-safe trade and
   funding ingestion ledger with stable venue IDs and exact ordering.
   The offline accounting calculator must not be fed guessed fills,
   incomplete pages, or synthetic funding completeness flags.
4. **Protective stop lifecycle:** implement and review idempotent native
   stop create/modify/cancel with persistent write-ahead state, atomic
   compare-and-swap, timeout/unknown-result recovery and post-write
   exchange verification. A read-only proposed amendment does not
   protect a live position.
5. **Account-wide reconciliation:** support non-flat restart recovery,
   duplicate or partially filled orders, manual intervention,
   liquidation/margin drift, and protection against unrelated orders.
6. **Operational controls:** independent kill switch, risk caps,
   API-permission checks, clock skew, stale books, alerts, restart
   health, on-call response and verified emergency flatten procedure.
7. **Shadow exercise:** use only read-only exchange credentials and
   paper/synthetic execution, replay failures, and collect sustained
   evidence for parity, alerting and recoverability.
8. **Independent review and explicit operator authorization:** only
   after all blockers are evidenced, request separate approval for a
   tiny limited-risk live pilot. Do not interpret 'go' or 'continue'
   as permission to place funded orders.

## Read-only schema discovery (operator-local; first redacted sample obtained)
Run locally with a MEXC API key restricted to **read-only** permissions,
using process environment variables `MEXC_API_KEY` and `MEXC_API_SECRET`:

```bash
T100_EXECUTION_MODE=paper python -m app.t100_live_preflight --schema-only
```

The output deliberately omits account balances, position IDs, symbols,
quantities, unknown field names and secret values; it reports only an
allowlisted field/type summary and non-authorizing parser compatibility
assessment. Do **not** share keys, full API payloads,
private request headers, or unredacted logs. An empty account yields no
field schema evidence. This is a discovery probe, not proof of pagination,
atomicity, API permissions, or safety for real orders. It has not been run
against the operator account in this PR.

## Current architecture boundaries
- `app/t100_live_guards.py` blocks the production paper portfolio
  from switching to live mode.
- `app/t100_live_lifecycle_audit.py` and
  `app/t100_live_recovery.py` perform read-only safety checks.
- `app/t100_live_stop_journal.py` persists stop intent and verifies
  observed native stop state; it **does not** issue amendments.
- `app/t100_live_shadow_replay.py` replays bounded, explicitly
  complete synthetic/redacted observation fixtures into the fail-closed
  shadow session and emits digest-only, non-authorizing evidence. It does
  **not** collect real MEXC data or certify a shadow soak.
- `app/t100_live_consistency.py` checks two independently complete
  inventory reads for canonical content equality and timestamp proximity.
  Identical consecutive reads reduce torn-snapshot risk but **cannot**
  prove venue atomicity or exclude intervening changes.
- `app/t100_live_schema_assessment.py` compares redacted field/type
  evidence with the current shadow parser's requirements. Missing or
  nullable fields are flagged, while empty inventories remain
  INCOMPLETE_SCHEMA_EVIDENCE. Even compatible shapes remain uncertified:
  no MEXC enum semantics, page completeness or atomicity is inferred.
- `app/t100_live_contract_evidence.py` creates shareable, allowlisted
  shape-only summaries, never account values or unknown field names.
- `app/t100_live_preflight.py` now uses strict raw GET inventory reads
  instead of legacy silently filtered helpers; orphan native stops block
  readiness and even apparently empty reads remain uncertified pending
  verified venue response contracts and pagination.
- `app/t100_live_readonly_probe.py` provides a GET-only strict
  MEXC probe for raw position responses and raw stop-list pages; it
  does not assume or certify exchange pagination response semantics,
  and has not been run against authenticated account data.
- `app/t100_live_pagination.py` validates explicit page metadata,
  consistent totals and unique row IDs across all pages; it intentionally
  requires an adapter to map a **verified** venue response shape. Even a
  complete count cannot prove an atomic account snapshot. The legacy
  `MexcTradeClient` inventory helpers silently filter malformed rows and
  `stop_orders` currently reads only page 1; **do not feed their output
  directly into live safety decisions**.
- `app/t100_live_shadow_session.py` requires strictly increasing
  observation timestamps and bounded gaps; any failed check permanently
  halts that in-memory session until manual intervention. It is not a
  persistent service and cannot independently prove feed completeness.
- `app/t100_live_shadow.py` reconciles one atomic, independently supplied
  inventory snapshot against managed short positions and native stops;
  returns SHADOW_ONLY and data-only trailing proposals, never orders.
  It is not a running service or an authenticated venue read.
- `app/t100_live_snapshot.py` validates independently supplied read-only
  inventory snapshots and rejects stale, incomplete or malformed reads;
  it does **not** itself establish pagination completeness or fetch venue data.
- `app/t100_live_accounting.py` computes deterministic FIFO realized
  short PnL for explicitly complete, ordered, linear USDT fills and
  funding events; it **does not** fetch or certify exchange history.
- `sql/t100_live_stop_amendments_draft.sql` is a **draft** schema, not
  an applied migration.

## What counts as ready
A green offline workflow alone is insufficient. The release requires
documented pass evidence for every gate above, a complete integration
test on actual read-only MEXC data, an operational shadow soak, and
separate explicit approval before any real order.


## 2026-10-24 operator redacted MEXC evidence

A schema-only authenticated read was run locally with the operator's key.
No orders were sent. The open-position endpoint returned one row and its
allowlisted types were compatible with the current shadow position parser
(positionId integer, symbol string, positionType integer, holdVol integer).
The current TP/SL endpoint returned zero rows, so open-stop schema remains
unobserved from the account.

The current official MEXC Futures documentation describes both the current
TP/SL endpoint and the historical TP/SL endpoint as returning data arrays.
For /api/v1/private/stoporder/list/orders, page_num/page_size are request
parameters but the documented response does not include total/page metadata.
Accordingly, a short/empty page must never be used as proof of pagination
completeness. The schema-only CLI now optionally samples finished TP/SL page
1 for redacted field/type evidence when no current stop row exists; that
sample remains explicitly non-complete and non-authorizing.


### 2026-10-24 second operator schema-only sample

The schema-only rerun returned two finished TP/SL rows from page 1. The
shareable evidence contained no account values. Observed allowlisted types:

- position: positionId=integer, symbol=string, positionType=integer,
  holdVol=integer; other observed numeric/time fields were integer/number.
- historical TP/SL: id=integer, positionId=string, state=integer,
  stopLossPrice=number, vol=integer, symbol=string, create/update times
  integer, profitLossVolType=string, volType/stopLossType=integer.

This is parser-shape compatible with the current defensive decoder, which
accepts string or integer identifiers and numeric strings/numbers for
volumes/prices. It remains non-authorizing evidence: the rows are FINISHED
history, not currently-open protection. In particular, active-stop state=1
semantics have not yet been observed from this account.

There is also a documentation/runtime type discrepancy worth preserving:
the current MEXC API documentation labels TP/SL positionId as long, while
the operator's finished TP/SL sample returned positionId as a string. The
parser therefore deliberately continues to accept both string and integer
IDs rather than narrowing to the documentation example.


### Raw double-read consistency discovery

Schema-only preflight now performs two dedicated consecutive GET-only reads
of current positions and current TP/SL orders and compares canonical hashes
inside the process. The shareable report exposes only whether the two reads
matched; it does not print the hash or account values.

A match is evidence against a torn read during that short interval only.
It explicitly reports raw_inventory_completeness_verified=false and does not
claim endpoint completeness, exchange atomicity, active stop semantics, or
live readiness.


### 2026-10-24 third operator schema-only sample: repeated inventory

The updated schema-only preflight completed successfully against the real
MEXC account and reported:

- raw_inventory_double_read_consistent=true
- raw_inventory_completeness_verified=false
- one current position row observed
- zero current TP/SL rows observed
- two finished historical TP/SL rows observed
- active_stop_contract_verified=false
- pagination_verified=false

This is evidence that two consecutive current position/current TP/SL reads
were identical during the observation window. It is not evidence that either
endpoint is complete or atomic.

The current MEXC Get Open Positions documentation defines position state as
1=holding, 2=system-held, 3=closed. The shadow inventory validator now
requires state=1 and fails closed on missing, system-held or closed states.
The schema assessment also requires the state field.

For T100 readiness, a current position with no current native TP/SL row is
now surfaced explicitly as
POSITION_WITHOUT_OBSERVED_CURRENT_TPSL and adds the readiness failure
t100_position_without_observed_native_tpsl. This does not assert that the
exchange endpoint is complete; it is a conservative strategy-specific block.

Official references:
- https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/get-open-positions
- https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/get-current-take-profitstop-loss-order-list


### 2026-10-24 fourth operator schema-only sample: current active TP/SL observed

After the operator independently added TP/SL to an already-existing real
position for normal trade management, the schema-only GET probe observed:

- one current position row
- one current TP/SL row from /api/v1/private/stoporder/open_orders
- raw_inventory_double_read_consistent=true
- active_stop_schema_observed=true
- stop_schema_source=open_orders
- both position and current TP/SL schemas parser-shape compatible
- pagination/completeness/atomicity still unverified

The observed current TP/SL row exposed the expected core fields used by the
shadow parser: id integer, positionId string, state integer, stopLossPrice
number, vol integer, symbol string, plus documented TP/SL metadata. No raw
account values are retained in the repository.

Current MEXC documentation for /api/v1/private/stoporder/open_orders defines:
state=1 untriggered; positionId links the order to a position; positionType
1/2 identifies long/short; vol is order quantity; isFinished=0 is non-final;
stopLossPrice is the SL trigger price. The preflight now performs an
in-process semantic assessment of the observed active rows and emits only
boolean/count evidence for state, non-final status, position linkage,
symbol/side consistency, positive protective price/volume, and observed
protective-volume coverage. This stronger evidence still does not certify
endpoint completeness, exchange atomicity, or live trading.


### 2026-10-24 fifth operator schema-only sample: active TP/SL transitioned to history

A subsequent read-only probe observed the position still present but zero
current TP/SL rows. Finished TP/SL page 1 increased from two rows to three.
The repeated current inventory read remained consistent.

This is evidence that the previously observed current TP/SL row moved out of
/open_orders and is now represented in finished history, but the schema-only
output alone cannot distinguish cancellation, execution, invalidation, or
execution failure.

MEXC documents finished stoporder states as:
2=canceled, 3=executed, 4=invalid, 5=execution failed, and triggerSide as
0=untriggered, 1=take-profit, 2=stop-loss. A new redacted transition
diagnostic therefore examines only finished rows linked to currently observed
position IDs, chooses the latest by updateTime/createTime, and exposes only
the terminal enum label, trigger-side label, finished flag, a coarse age
bucket, and counts. It never returns IDs, symbols, prices, volumes, or raw
timestamps.

This diagnostic is observational only and still does not certify endpoint
completeness, exchange atomicity, or live readiness.


### 2026-10-24 sixth operator sample: cancellation transition confirmed by operator ground truth

The operator confirmed that the previously observed current TP/SL was
manually canceled. The subsequent GET-only transition diagnostic reported:

- latest_terminal_state=CANCELED
- latest_trigger_side=UNTRIGGERED
- latest_is_finished=true
- transition_semantics_observed=true
- exactly one finished TP/SL row linked to the still-observed position

This is direct real-account confirmation of the cancellation lifecycle
mapping for the observed MEXC contract: a current TP/SL row disappeared from
/open_orders after manual cancellation and appeared in finished history with
the documented canceled/untriggered semantics. The coarse age bucket was
OLDER, so no claim is made about transition timing beyond the stored terminal
state.

This closes the observed cancellation-state semantic evidence gap. It does
not certify TP/SL execution/trigger semantics, endpoint completeness, or
exchange atomicity.


### 2026-10-24 seventh operator sample: funding pagination verified on real position

The schema-only funding probe collected 489 funding rows for the one observed
position across five pages. The live MEXC response supplied explicit
pageSize/totalCount/totalPage/currentPage/resultList metadata and the strict
collector verified:

- reported_total_rows=489
- page_count=5
- pagination_verified_for_observed_position=true
- no duplicate row identities
- schema fields matched documented funding fields
- account-wide funding completeness remains unverified

This closes position-scoped funding pagination evidence for the observed
position.

Funding economics remain a separate semantic layer. MEXC's current funding
rules state that positive funding rates transfer from longs to shorts and
negative rates transfer from shorts to longs. The current funding-record API
exposes positionType, positionValue, funding and rate. The next redacted
semantic check compares every non-zero row against the expected cashflow
direction and checks fee magnitude against positionValue*rate within a small
rounding tolerance. Only counts/booleans are emitted; funding amounts,
symbols, position IDs and timestamps are never shared.


### 2026-10-24 eighth operator sample: funding economics fully consistent

The funding-economics probe re-read the complete 489-row, five-page funding
history for the observed position and reported:

- nonzero_rows_examined=489
- sign_consistent_rows=489
- magnitude_consistent_rows=489
- all_nonzero_rows_sign_consistent=true
- all_nonzero_rows_magnitude_consistent=true
- funding_cashflow_sign_semantics_observed=true
- funding_formula_semantics_observed=true
- status=FUNDING_SIGN_AND_FORMULA_CONSISTENT

This is real-account evidence that the signed MEXC funding field behaves as
account cashflow for the observed rows and follows the documented stablecoin
perpetual rule: positive rates debit longs/credit shorts, negative rates credit
longs/debit shorts, with magnitude consistent with positionValue*rate.

Together with the prior five-page pagination proof, this closes
position-scoped funding ingestion semantics for the observed position.
Account-wide completeness remains unverified.

### Order-specific fill ingestion design

Current MEXC documentation exposes two GET-only endpoints suitable for exact
fill reconciliation:
- GET /api/v1/private/order/get/{orderId}
- GET /api/v1/private/order/deal_details/{orderId}

The order response supplies dealVol/dealAvgPrice/symbol/side/state and the
deal-details response supplies immutable trade IDs, orderId, symbol, side,
vol, price and signed fee. MEXC documents fee as positive when paid by the
user and negative when received. The offline accounting layer now accepts
signed fill fees, including rebates.

A new privacy-preserving order-specific fill reconciler requires all fill
order IDs, symbols and sides to match the immutable order; requires distinct
trade IDs; requires summed fill volume to equal order dealVol; reconciles
volume-weighted fill price against dealAvgPrice; and requires consistent fee
currency. No broad account history is used to infer ownership of an order.
Real fill evidence will require a known order ID that T100 owns or that the
operator deliberately designates for audit; no order will be created for
testing.


### 2026-10-24 durable non-flat restart recovery design

The previous restart gate intentionally blocked every non-flat account because
the existing live order-intent ledger does not persist enough runtime state to
reconstruct a managed T100 position after a crash. In particular it does not
durably record position_id, native stop_order_id/current stop_price, or the
trailing state best_profit_pct/armed.

A separate draft-only checkpoint schema was therefore added at
sql/t100_live_position_checkpoints_draft.sql. It is NOT deployed.

The new pure recovery policy in app/t100_live_durable_recovery.py reconstructs
ManagedShort state only when every condition below is true:

- 1..4 OPEN durable checkpoints exist, one symbol per checkpoint
- checkpoint trailing state is internally coherent
- each checkpoint has exactly one ACK order intent with matching immutable
  external/exchange order identity, initial quantity and initial stop bound
- no PREPARED/SUBMITTING/UNKNOWN/REVIEW order intent exists
- no unresolved stop amendment exists
- the original MEXC order is re-read and matches externalOid, orderId, symbol,
  OPEN_SHORT side, final-filled state, positionId, dealt quantity and average
  fill price
- current position inventory is independently certified complete
- current TP/SL inventory is independently certified complete
- every exchange position exactly matches checkpoint identity, short side,
  holding state, quantity and entry price
- every native stop exactly matches checkpoint identity, active state,
  stop price and protective quantity
- no additional exchange position, stop or ACK intent exists outside the
  durable checkpoint set

Recovery output remains SHADOW_ONLY_RECOVERED_NONFLAT with
live_authorization=false. Any mismatch fails closed.

A SELECT/GET-only orchestration layer loads checkpoints, intents and stop
amendments and re-reads the immutable origin order plus current positions and
native stops. It defaults both endpoint-completeness flags to false; callers
must supply independently established completeness evidence.

Current MEXC documentation describes open positions and current TP/SL as
non-paginated list responses, but does not provide an explicit maximum,
truncation contract, total count, or atomic-snapshot guarantee. Therefore the
current real probe still cannot set positions_complete/stops_complete=true
solely because expected rows were observed. Durable recovery remains
non-authorizing until that evidence gap is resolved.

The draft checkpoint table is intentionally not migrated and no live database
writes were performed.


### 2026-10-24 bounded read-only soak and operational kill controls

A new one-way OperationalKillLatch is shared by the soak/risk safety layer.
It preserves the first trip reason, increments diagnostic trip count on later
trips, exposes no reset/clear/enable method, and never grants entry or live
authorization.

Two soak evidence levels are intentionally separated:

1. Raw discovery soak
   - bounded by sample count and wall-clock timeout
   - uses only authenticated GET reads of open positions/current TP-SL
   - each sample requires two consecutive canonical raw inventory reads to
     match within a bounded separation
   - reports position/TP-SL presence counts and cross-sample count changes
   - does NOT treat a cross-sample lifecycle transition as corruption because
     it has no authoritative local lifecycle model
   - explicitly keeps raw_inventory_completeness_verified=false,
     exchange_atomicity_verified=false, entry_allowed=false and
     live_authorization=false
   - a within-pair drift, transport failure or timeout trips the local kill
     state and stops the soak

2. Operational soak
   - requires independently complete position and TP-SL inventory booleans
   - consumes exact expected ManagedShort state through the existing
     fail-closed shadow reconciler
   - requires fresh, monotonic observations with a bounded gap
   - missing protection trips NATIVE_PROTECTION_LOST
   - extra/changed protection trips NATIVE_PROTECTION_DRIFT
   - position count/identity/quantity drift trips POSITION_DRIFT
   - stale data, observation gaps, malformed expected state or other
     reconciliation ambiguity trip a permanent kill reason
   - once tripped, a later healthy observation cannot resume the same process
   - even after the configured observation threshold is reached, status is
     SOAK_THRESHOLD_MET_NON_AUTHORIZING and both entry_allowed and
     live_authorization remain false

A dedicated operator-local CLI app/t100_live_soak_cli.py runs only the raw
discovery layer. It refuses non-paper T100 execution mode, performs no DB
writes and no POST requests, and emits only counts/booleans/timing/status.
The default configuration is 12 samples, five seconds apart, with a bounded
120-second overall runtime.

Quantitative operational risk controls were also added:
- manual kill switch
- maximum per-position notional
- maximum total portfolio notional
- maximum daily loss
- exact reconciliation of reported portfolio total against position totals
- malformed/unreconciled risk inputs themselves trip RECOVERY_UNVERIFIED

All quantitative kills are one-way in-process. A later below-limit value
cannot clear an earlier breach.

These controls remain non-authorizing and are not wired to live order
submission. They define prerequisites for a future coordinator.

Current real-account state still has an observed position with zero current
TP/SL rows following the operator's manual cancellation. A raw discovery
soak may record that condition, but it cannot be interpreted as an
operational-protection pass.


### 2026-10-24 first raw discovery soak: full-row drift exposed a false-positive gate

The first bounded real-account discovery soak requested 12 samples but halted
after two completed samples. Both completed samples had matching consecutive
raw reads, one position was present, no current TP/SL was present, and the
third pair tripped the original full-payload fingerprint with
INVENTORY_CHANGED_BETWEEN_READS.

This halt was not treated as proof of position identity/quantity drift. MEXC's
current open-position contract includes naturally moving fields such as
unRealizedPnl, marginRatio, funding/fee-related fields and other current
position economics in the same row as stable inventory identity fields.
Hashing the complete row therefore conflated mark-to-market/accounting drift
with lifecycle inventory drift.

The discovery soak now computes two comparisons for each pair:

- a strict safety-critical projection, used as the kill gate
- the full raw response, retained as diagnostic drift evidence only

Critical position projection includes identity, symbol, side/state, quantity,
open type, frozen/closed quantity, entry/open averages, leverage, initial
margin/auto-add settings and position provenance fields. Critical TP/SL
projection includes stop/order/position identity, state/finality, trigger
side, position side, trigger prices, quantity semantics/types, price
protection and reverse/limit semantics.

Naturally volatile fields remain visible through:
- full_raw_drift_samples
- changed_position_field_names
- changed_stop_field_names

No values are emitted.

A change in the critical projection still trips
INVENTORY_CHANGED_BETWEEN_READS. A full-row-only change is counted and the
bounded discovery soak continues. This does not upgrade completeness or
atomicity evidence.

The revised diagnostic is version
2026-10-24-projected-discovery-soak-v2.


### 2026-10-24 projected discovery soak v2: 12/12 critical pairs stable

The revised bounded real-account discovery soak completed all 12 requested
samples over approximately 69 seconds.

Observed result:
- completed_samples=12
- consistent_samples=12
- cross_sample_inventory_count_changes=0
- position_presence_samples=12
- current_tpsl_presence_samples=0
- position_without_current_tpsl_samples=12
- kill_tripped=false
- full_raw_drift_samples=4
- changed_position_field_names={unRealizedPnl}
- changed_stop_field_names={}
- raw_inventory_completeness_verified=false
- exchange_atomicity_verified=false
- entry_allowed=false
- live_authorization=false
- status=COMPLETED_READ_ONLY_DISCOVERY

This confirms that the first soak halt was caused by naturally changing
unrealized PnL rather than safety-critical inventory drift. Across all 12
pairs the critical projection remained stable.

This is sustained read-only discovery evidence only. The observed account
still had one position and zero current TP/SL rows following the operator's
manual TP/SL cancellation, so this cannot be counted as a protected-position
operational soak.


### 2026-10-24 symbol-scoped current-inventory redundancy cross-check

A second GET-only consistency layer now compares the account-wide current
position/TP-SL lists against the venue's symbol-filtered reads for every
currently observed symbol, bounded to at most four symbols.

For each observed symbol the checker compares only the same safety-critical
projection used by projected discovery soak. Naturally volatile fields such
as unrealized PnL therefore do not create false mismatches.

The cross-check verifies:
- every aggregate current position row for an observed symbol is reproduced
  by the symbol-filtered open-positions request with matching critical fields
- every aggregate current TP/SL row for an observed symbol is reproduced by
  the symbol-filtered current-TP/SL request with matching critical fields
- filtered position rows cannot silently return a different symbol
- stop rows with a symbol field cannot silently return a different symbol
- more than four observed symbols refuses the bounded check rather than
  expanding private-read load unexpectedly

Shareable output exposes only counts and booleans:
- symbols_checked
- aggregate_positions
- aggregate_current_tpsl
- position_symbol_scoped_match
- current_tpsl_symbol_scoped_match
- symbol_scoped_crosscheck_verified

It explicitly leaves:
- account_wide_completeness_verified=false
- exchange_atomicity_verified=false

This redundancy test is not a substitute for a documented total/truncation
contract. It can detect an inconsistency between two parameterizations of the
current inventory API but cannot mathematically exclude an unseen symbol that
the aggregate endpoint omitted.


### 2026-10-24 symbol-scoped redundancy and 491-row funding refresh

A real schema-only preflight with diagnostic version
2026-10-24-symbol-redundancy-v1 observed:

- aggregate_positions=1
- aggregate_current_tpsl=0
- symbols_checked=1
- position_symbol_scoped_match=true
- current_tpsl_symbol_scoped_match=true
- symbol_scoped_crosscheck_verified=true
- account_wide_completeness_verified=false
- exchange_atomicity_verified=false
- status=SYMBOL_SCOPED_REDUNDANCY_MATCH

This confirms that the account-wide current-position row is reproduced by the
symbol-filtered position request on all safety-critical fields and that both
aggregate and symbol-filtered current TP/SL reads return zero rows for the
observed symbol.

The same preflight re-read funding history. The reported position-scoped
history advanced naturally from 489 to 491 rows across the same five verified
pages. All 491 non-zero rows remained sign-consistent and formula-consistent.

The preflight's legacy raw_inventory_double_read_consistent=false was caused
by the same full-row unrealized-PnL drift already isolated by projected soak.
The schema-only preflight has now been aligned with projected consistency and
reports critical_inventory_double_read_consistent separately from full raw
payload equality. Full raw drift no longer creates a false critical-inventory
failure.

### 2026-10-24 private WebSocket continuity observer

Current MEXC Futures documentation exposes authenticated private WebSocket
push channels for:
- push.personal.position
- push.personal.stop.planorder
- push.personal.order.deal

The documented Futures WS endpoint is wss://contract.mexc.com/edge.
Private login signs accessKey+timestamp with HMAC-SHA256. Login can disable
default pushes and personal.filter can limit subscriptions to position,
stop.planorder and order.deal.

A bounded read-only observer was added. It:
- authenticates only
- filters to those three private channels
- sends heartbeat ping messages
- accepts JSON or gzip-compressed frames
- records only channel counts and field names/types
- never emits symbols, IDs, prices, quantities, fees, credentials or raw
  private payloads
- performs no REST POST/DELETE, order submission/cancellation, stop mutation
  or database write
- remains entry_allowed=false and live_authorization=false

The private WS stream is independent continuity evidence only. It does not
prove the initial REST snapshot is account-wide complete or atomic.


### 2026-10-24 projected schema-only preflight v2: corrected consistency pass

A real schema-only preflight with diagnostic version
2026-10-24-projected-preflight-v2 reported:

- critical_inventory_double_read_consistent=true
- raw_inventory_double_read_consistent=true
- changed_position_field_names={}
- changed_stop_field_names={}
- symbol_scoped_crosscheck_verified=true
- aggregate_positions=1
- aggregate_current_tpsl=0
- position_symbol_scoped_match=true
- current_tpsl_symbol_scoped_match=true
- funding reported_total_rows=491
- funding pagination_verified_for_observed_position=true
- funding sign_consistent_rows=491/491
- funding magnitude_consistent_rows=491/491
- raw_inventory_completeness_verified=false
- account-wide completeness remains unverified
- exchange atomicity remains unverified
- live_order_submission_possible=false
- read_only=true

This confirms the corrected preflight no longer conflates unrealized-PnL movement
with safety-critical inventory drift. On this specific pair even the complete
raw payload was identical.

The account still had one observed position and zero current TP/SL rows after
the operator's manual cancellation, so active protection semantics remain
insufficient for an operational-protection pass.


### 2026-10-24 first real private WebSocket observation: authenticated quiet pass

A 30-second operator-local private Futures WebSocket observation completed
successfully with diagnostic version
2026-10-24-private-ws-observer-v1.

Observed result:
- credential_status=PRIVATE_WS_LOGIN_SUCCEEDED_READ_ONLY_OBSERVATION
- login_verified=true
- filter_sent=true
- pongs_seen=1
- private_events_seen=0
- position_events_seen=0
- stop_plan_events_seen=0
- fill_events_seen=0
- unexpected_private_channels={}
- elapsed_ms=30628
- status=COMPLETED_PRIVATE_WS_OBSERVATION
- read_only=true
- live_order_submission_possible=false
- entry_allowed=false
- live_authorization=false
- rest_snapshot_completeness_verified=false
- exchange_atomicity_verified=false

This validates the real-account private WS authentication/filter/heartbeat
path and a clean bounded observation interval. Zero private events during the
window is not treated as evidence that no position exists and does not
validate position/stop/fill event payload semantics. Those remain unobserved
until a real account lifecycle event naturally occurs.

No account mutation was performed for the purpose of generating a WS event.


### 2026-10-24 REST-WS-REST continuity sandwich probe

A bounded read-only continuity probe was added after the successful private WS
quiet observation.

The probe performs:
1. strict REST current positions + current TP/SL read
2. bounded authenticated private WS observation
3. strict REST current positions + current TP/SL read
4. comparison of safety-critical REST projections before vs after

It separately reports complete raw-field drift names so naturally volatile
fields can change without masking critical inventory continuity.

Possible outcomes distinguish:
- REST_WS_REST_CONTINUITY_STABLE
- STABLE_REST_WITH_PRIVATE_WS_EVENTS_OBSERVED
- STABLE_REST_WITH_UNEXPECTED_PRIVATE_WS_CHANNELS
- CRITICAL_REST_INVENTORY_CHANGED_DURING_WS_WINDOW

The report is redacted and non-authorizing. It exposes only counts, booleans,
field names/types and status. It does not prove account-wide completeness or
exchange atomicity.


### 2026-10-24 first real REST-WS-REST continuity sandwich: stable

A 30-second real-account continuity probe completed with:

- before_positions=1
- after_positions=1
- before_current_tpsl=0
- after_current_tpsl=0
- critical_positions_equal=true
- critical_current_tpsl_equal=true
- critical_rest_inventory_equal=true
- full_raw_positions_equal=false
- changed_position_field_names={unRealizedPnl}
- changed_stop_field_names={}
- ws login_verified=true
- ws filter_sent=true
- ws pongs_seen=1
- ws private_events_seen=0
- ws unexpected_private_channels={}
- status=REST_WS_REST_CONTINUITY_STABLE
- account_wide_completeness_verified=false
- exchange_atomicity_verified=false
- live_authorization=false

This is independent continuity evidence spanning both REST and the authenticated
private WS channel. It confirms that the safety-critical current inventory did
not change across the WS observation window while the expected mark-to-market
field moved.

No current TP/SL existed before or after the window following the operator's
manual cancellation, so this is not protected-position operational soak
evidence.


### 2026-10-24 bounded multi-window continuity soak

A bounded multi-window REST-WS-REST continuity soak is now implemented.

Default/manual pilot configuration:
- 5 windows
- 30-second private WS observation per window
- 2-second pause between windows

Each window reuses the same read-only continuity sandwich and must either:
- REST_WS_REST_CONTINUITY_STABLE, or
- STABLE_REST_WITH_PRIVATE_WS_EVENTS_OBSERVED

A coherent private lifecycle event is not treated as failure; it is counted
and the soak continues if the before/after critical REST state remains
coherent.

The soak halts on the first:
- critical REST inventory change
- unexpected private WS channel
- transport/probe exception
- other non-pass window status

It accumulates only redacted evidence:
- completed/stable windows
- critical change count
- WS event windows/event count
- heartbeat count
- full-raw drift-window counts
- changed position/stop field names
- first failure status

It remains:
- account_wide_completeness_verified=false
- exchange_atomicity_verified=false
- entry_allowed=false
- live_authorization=false

Offline safety suite: 456 tests passed on the final multi-window soak head.


### 2026-10-24 real multi-window REST-WS-REST continuity soak: 5/5 stable

The bounded real-account continuity soak completed all five requested windows.

Observed result:
- requested_windows=5
- completed_windows=5
- stable_windows=5
- critical_change_windows=0
- first_failure_status=null
- full_raw_position_drift_windows=3
- full_raw_stop_drift_windows=0
- changed_position_field_names={unRealizedPnl}
- changed_stop_field_names={}
- total_ws_pongs=5
- total_ws_private_events=0
- ws_event_windows=0
- status=COMPLETED_CONTINUITY_SOAK_NON_AUTHORIZING
- account_wide_completeness_verified=false
- exchange_atomicity_verified=false
- entry_allowed=false
- live_authorization=false

This closes the sustained read-only REST+private-WS continuity evidence item:
all five windows preserved identical safety-critical position/TP-SL state and
the authenticated WS channel remained healthy in every window. The only
complete-raw-position drift was unrealized PnL, consistent with the earlier
projected-soak diagnosis.

This still does not establish account-wide completeness/atomicity and does not
validate real private WS lifecycle payload semantics because no private event
occurred during the soak.


### 2026-10-24 durable checkpoint persistence protocol implemented offline

The durable non-flat restart design now has a complete draft persistence side.
It remains intentionally undeployed.

Draft schema now contains:
- t100_live_position_checkpoints current state
- t100_live_position_checkpoint_revisions append-only revision history

Every revision stores the complete recovery snapshot and one explicit reason:
- ENTRY_PROTECTED
- STOP_VERIFIED
- REVIEW_LATCHED
- CLOSING_LATCHED
- CLOSE_VERIFIED

Persistence invariants:
- exchange evidence must be verified before checkpoint advancement
- current checkpoint + append-only revision commit in one DB transaction
- updates use checkpoint_version/lifecycle compare-and-swap
- versions advance exactly by one
- immutable entry/order/position/stop identity cannot drift
- short stops never loosen
- trailing best-profit never regresses
- armed trailing state can never disarm
- unresolved mutation outcomes never advance the checkpoint
- REVIEW and CLOSING are deliberately non-recoverable states
- final CLOSE_VERIFIED atomically updates the checkpoint to CLOSED, advances
  the originating order intent ACK->CLOSED, and appends the close revision

Restart recovery now requires the full revision chain rather than trusting only
the mutable current row. For every recoverable OPEN checkpoint it requires:
- exact revisions 1..checkpoint_version with no gaps or duplicates
- version 1 reason ENTRY_PROTECTED
- later OPEN revisions reason STOP_VERIFIED
- immutable provenance across every revision
- strictly tightening stop history
- non-regressing trailing progress
- coherent armed state
- final revision identical to the current checkpoint

A persistence-only coordinator was added for initial protected entry, verified
stop advancement, REVIEW latch, CLOSING latch and verified CLOSED state.
Initial/stop/close persistence requires all four external evidence gates:
- evidence_fresh=true
- account_reconciled=true
- positions_complete=true
- stops_complete=true

Because current MEXC discovery still cannot set account-wide positions/stops
completeness true, the coordinator is intentionally unusable for real live
checkpoint writes today.

No migration was applied and no database or exchange state was modified.

Offline safety suite: 500 tests passed. All four branch workflows passed.


### 2026-10-24 account-level position-margin economic witness design

Current MEXC documentation exposes:
- GET /api/v1/private/account/assets with account-level positionMargin and
  unrealized by settlement currency
- open-position rows with per-position im and unRealizedPnl

MEXC's current futures calculation guidance describes available balance in
terms of wallet balance minus position margin/order margin and describes
unrealized PnL as the floating PnL of open positions.

A redacted read-only economic witness now compares:
- sum(observed USDT position im) vs USDT asset positionMargin
- sum(observed USDT position unRealizedPnl) vs USDT asset unrealized
- returned non-USDT asset rows for zero positionMargin
- returned non-USDT asset rows for zero unrealized

Because unrealized PnL can move between REST calls, two evidence levels are
reported separately:
- position_inventory_margin_witness_observed: stable margin reconciliation
- position_inventory_economic_witness_observed: margin + unrealized both
  reconcile in the sampled reads

The margin witness requires:
- exactly one returned USDT asset row
- all observed positions are USDT-settled
- observed position im sum matches account USDT positionMargin
- every returned non-USDT asset row has zero positionMargin

The output contains only booleans/counts/status and never account balances,
symbols, margin amounts or PnL values.

Even a successful witness explicitly leaves:
- account_wide_completeness_verified=false
- exchange_atomicity_verified=false

This is an independent detector for omitted position exposure, not yet a
formal replacement for a venue-documented list-completeness contract.

Offline safety suite: 515 tests passed; all four branch workflows passed.

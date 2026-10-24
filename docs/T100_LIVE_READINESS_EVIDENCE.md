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

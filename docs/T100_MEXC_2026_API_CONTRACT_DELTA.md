# T100/V40 — MEXC 2026-10-23 API contract delta and native SL checks

**Development-only / NO LIVE MUTATIONS — November 3, 2026.**

## Confirmed public API evidence

MEXC's official Futures API update log dated **2026-10-23** says
`POST /api/v1/private/order/create` added `sideType`
(`buy`/`sell`), modified/added `positionType`
(`long`/`short`/`both`), and documented positionMode=2 one-way
semantics. Current request documentation marks `sideType` required.

- https://www.mexc.io/api-docs/futures/update-log
- https://www.mexc.io/api-docs/futures/account-and-trading-endpoints/place-order

The T100 adapter's **unexecuted** OPEN_SHORT payload now declares
`side=3`, `sideType=sell`, `positionType=short`,
`positionMode=1` (hedge) and `openType=2` (cross), preserving the
existing mandatory order origin and initial stop-loss reference.
This corrects a documented missing field. It does NOT establish that
the venue will accept the request, atomically activate SL protection,
permit CROSS mode for the target instrument, fill at a safe price, or
return a complete private account snapshot. The repository still pins
`LIVE_ADAPTER_STATUS=NOT_IMPLEMENTED`.

Current official TP/SL docs describe the following:
- `GET /api/v1/private/stoporder/open_orders` exposes a stop order
  `id`, `positionId`, `state=1` untriggered,
  `isFinished=0`, `positionType=2` short,
  `stopLossType=0` **market SL** vs `1` **limit SL**,
  `stopLossReverse=2` **do not reverse**, `lossTrend=1`
  last-price trigger, and quantity modes `volType` /
  `profitLossVolType`.
- `POST /api/v1/private/stoporder/change_price` is documented as
  **modify TP/SL on a LIMIT ORDER** with `orderId`, and is NOT
  equivalent to modifying a position native stop by its TP/SL `id`.
- The old MEXC contract reference documents
  `POST /api/v1/private/stoporder/change_plan_price` with
  `stopPlanOrderId`, but that contract's semantics for the currently
  returned position TP/SL `id` have NOT been independently verified
  against the current official endpoint reference and actual venue.
  Reusing it as an assumed native position-stop amendment is unsafe.

Sources:
- https://www.mexc.io/api-docs/futures/account-and-trading-endpoints/get-current-take-profitstop-loss-order-list
- https://www.mexc.co/api-docs/futures/account-and-trading-endpoints/modify-tpsl-prices-on-a-limit-order
- https://mexcdevelop.github.io/apidocs/contract_v1_en/

## New fail-closed safeguards

1. `LIVE_NATIVE_STOP_AMENDMENT_STATUS` is independently pinned to
   `UNVERIFIED_CURRENT_DOCS`; the old native-stop mutation adapter
   refuses to call its `change_plan_price` POST even when a separate
   tiny-pilot permit has been constructed, unless this independent
   endpoint certification flag has been explicitly updated in
   reviewed source. The global adapter also remains NOT_IMPLEMENTED.
2. `assess_native_short_market_stop_row` is a separate, pure read-only
   audit of supplied current MEXC GET fields. It requires exact native
   order/position identity, current state, short side, **market**
   stopLossType=0, no reverse, latest-price loss trigger, a positive
   protective stop not looser than the strategy bound, and full
   effective protective quantity. Unknown/missing types, limit-stop,
   reverse order, wrong trigger, stale/inactive row, wrong side,
   manual/mismatched position and partial quantity are all blockers.
3. The audit has deliberately false
   `stop_execution_certified`,
   `exchange_atomicity_certified`, `entry_allowed` and
   `live_authorization` even when the static row looks fully
   compatible. No signed live venue stop response has been used
   to certify the strategy and no native stop POST was sent.

Only an independent current MEXC endpoint/ID mapping, authorized
small venue experiment under a separate release gate, verified
native-stop placement/amendment/cancel+GET lifecycle, gap/latency
and account collateral stress testing can close this blocker.
A stop-market order is **not** a guaranteed fill price and does
not prevent liquidation before trigger.

## Live-release disposition

- The T100 production adapter is **NOT CERTIFIED** for real trading.
- The new stop-amendment endpoint check is an explicit blocker, not
  a credential switch or implied authorization.
- The audit does not automatically certify previously persisted V40
  checkpoints or existing manual positions. It produces only an
  advisory static field witness.
- Read-only shadow deployment still requires independent configuration
  and safe rollout review.
- No MEXC POST, native stop edit, production DB migration/role change,
  Render deployment, credential change, or BOXWAVE change occurred.

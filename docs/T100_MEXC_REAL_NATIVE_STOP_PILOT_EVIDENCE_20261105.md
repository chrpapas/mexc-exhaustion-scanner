# T100/225 MEXC native-stop real pilot evidence — 2026-11-05

**Status:** read-only incident interpretation and regression recorded. **NOT live-authorizing.**
Account: *dedicated $50 T100 macOS Keychain subaccount*, **not** the existing
Render shadow account and **not** BOXWAVE. No credentials, IDs or private order
prices are stored here.

## Confirmed in signed GETs after real V5 POST

- An isolated 1x single-contract BTC_USDT short entry filled.
- An active position-origin, full-position native market stop was independently
  observed before the position became flat.
- No V5 journal fallback-close POST marker was present in the observed
  stop branch. This does not on its own prove absence of outside manual activity.
- The exact original native TP/SL record is terminal `state=3` (executed),
  `isFinished=1`, `positionType=2` (short), `stopLossType=0` (market SL),
  `volType=2` (entire position), `errorCode=0`, `orderId=0` (position origin).
- The stop record's `placeOrderId` independently matches a filled one-contract
  `CLOSE_SHORT` `MARKET` order for that exact position.
- Exchange inventory was independently confirmed flat with no open orders.

## Material venue conflict — DO NOT OVERRIDE

The **same completed TP/SL record has `triggerSide=1`**. MEXC officially
documents `1=take-profit` and `2=stop-loss`. Thus a filled, venue-generated
close is proven, but a **stop-loss-side trigger is NOT certified**.

The stop configuration and result do not resolve whether this is an exchange
classification defect, an unexpected positive take-profit condition, or
another exchange semantic. Do not 'repair' it by treating `state=3` or
`stopLossType=0` alone as evidence `triggerSide=2`.

## Production action taken

- Added `app/t100_live_native_stop_terminal_audit.py`: read-only, strict,
  no-secret/no-POST terminal-lifecycle evaluator.
- Added `tests/test_t100_live_native_stop_terminal_audit.py`: regression for
  the **actual** mismatched TP-side terminal event, separate from a valid
  stop-loss-side mock event. A filled close does NOT grant live authorization.
- No existing Render service altered, no existing safety latch reset, no
  trading gateway or live-order capability enabled.

## Remaining gate

Before authorizing live execution, obtain either:
1. A completed real exchange case with `triggerSide=2` **and** filled
   matched `placeOrderId`, or
2. A documented exchange clarification establishing different semantics,
   followed by independently reviewed revised validation.

Also independently certify interruption recovery, emergency close,
funded CROSS/four-slot envelope and Render monitoring. Avoid repeated
live pilots solely to satisfy an ambiguous field; inspect the
existing `takeProfitPrice` and exact event with MEXC support first.

Official MEXC field reference:
https://www.mexc.io/api-docs/futures/account-and-trading-endpoints/get-take-profitstop-loss-order-list

## V4 read-only verification: economic loss direction and no configured TP

Further signed GET forensics on November 5, 2026 recorded:
- Reviewed V5 source hash: POST request carried a `stopLossPrice` only,
  with **no** `takeProfitPrice` field.
- Finished MEXC TP/SL record: `takeProfitPrice` field **MISSING**
  (this is *not* the same as an explicitly zero value); `stopLossPrice`
  was positive and equal to the reviewed intended stop.
- SHORT entry was fully filled. Configured SL was **above** the entry
  average. Linked exchange-generated CLOSE_SHORT market order filled
  **above** the entry average, a loss-making price move for a short.
- Neither the original V5 journal stop branch nor the exchange records
  indicate a separate V5 fallback close POST; flat/no-open-orders
  independently checked twice.
- Yet the finished TP/SL event reports `triggerSide=1` ("take-profit")
  instead of documented `2` ("stop-loss"). **Do not equate economic
  stop-loss consistency with venue stop-loss trigger certification.**
- New regression explicitly records economic consistency with a
  contradictory label, and keeps `stop_loss_trigger_proven=false`,
  `entry_allowed=false`, `live_authorization=false`.

### Support inquiry for the official MEXC Futures API channel

Official MEXC technical support: https://t.me/MEXCAPIsupport
Official documented semantics:
https://www.mexc.io/api-docs/futures/account-and-trading-endpoints/get-current-take-profitstop-loss-order-list

> **Subject: Futures API `triggerSide=1` on stop-loss-only short position order**
>
> On November 5, 2026 (UTC), we used
> `POST /api/v1/private/stoporder/place` to create a position-origin
> full-position market stop loss for a 1-contract BTC_USDT isolated short.
> The request included `stopLossPrice` but no `takeProfitPrice`.
> Active stop protection was verified before the position closed.
>
> The finished record returned by
> `GET /api/v1/private/stoporder/list/orders` has
> `state=3`, `isFinished=1`, `stopLossType=0`,
> `volType=2`, `errorCode=0`, `orderId=0`,
> and a `placeOrderId` matching a fully filled 1-contract
> market CLOSE_SHORT. The finished record has a positive stopLossPrice
> and **no takeProfitPrice field**. The stop price and market close
> execution price were both above the short's average entry price.
> Nevertheless, `triggerSide=1` (documented as take-profit), not 2
> (documented as stop-loss).
>
> Please confirm whether `triggerSide=1` is a known incorrect
> classification for a stop-loss-only order. If not, what precise
> event triggered this position-origin TP/SL record, and what alternate
> authenticated API evidence can independently prove that the
> **stop-loss side** triggered and filled?
> We can supply the exact stop ID, position ID, generated close order ID,
> and timestamps privately through the secure support channel.
>
> No API credentials, headers, signatures or passwords will be shared.

## November 6, 2026 — Independent real V5 terminal position confirmation

The operator ran the reviewed local `t100_mexc_v5_closed_position_four_get_local.py`
on the **dedicated T100 pilot subaccount**, reading the original V5
journal without modifying it and issuing two signed history GETs plus two
exact position-ID GETs. The returned redacted report was:

```text
FORENSIC_MODE=EXISTING_V5_JOURNAL_FOUR_SIGNED_GET_ONLY
HISTORY_RESPONSE=ARRAY_WITHOUT_PAGINATION_METADATA
V5_EXACT_CLOSED_POSITION_HISTORY=V5_EXACT_CLOSED_POSITION_POSITIVE_HISTORY_MATCH
V5_CLOSED_POSITION_VERIFIED=true
FULL_HISTORY_CERTIFIED=false
WHOLE_ACCOUNT_FLAT_CERTIFIED=false
STOP_LOSS_TRIGGER_SIDE_PROVEN=false
LIVE_AUTHORIZATION=false
MEXC_ORDERS_SENT=0
DATABASE_MUTATIONS=0
REQUIRES_MANUAL_POSITION_MANAGEMENT_CHANGE=false
```

This is positive independent evidence that the **exact original V5 position**
closed. It is not a proof of whole-account flatness; four historical rows
on page 1 are not an independently complete history. MEXC's venue
`triggerSide=1` (documented take-profit) contradiction is unchanged and
the STOP-LOSS-SIDE **trigger itself is not certified**. No V5 rerun or
further live order was made.

### Production recovery bridge (development-only)

`app/t100_live_native_stop_close_recovery.py` is a GET-only recovery
path distinct from the **guardian's emergency-close** recovery. It now
requires all four specific evidence links: originating T100-filled entry,
exact finished position-origin native stop, its generated full-volume
market CLOSE_SHORT and a positive exact closed-position historical
record with matching targeted-position GETs. Finished TP/SL history has
no independent page count: an absent row is **not proof** the stop never
existed or executed. The bridge retains both the venue's contradictory
take-profit trigger label and the proof of a venue-generated close.
It performs **zero exchange mutations, zero checkpoint writes, zero
manual-position adoption, and never authorizes new live entries**.
The actual-account V5 journal is a local forensic source, not an
account-enrolled durable production checkpoint.

This is engineering evidence, **not** production certification or
a justification to clear the existing Render kill latch.

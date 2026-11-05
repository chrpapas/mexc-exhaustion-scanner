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

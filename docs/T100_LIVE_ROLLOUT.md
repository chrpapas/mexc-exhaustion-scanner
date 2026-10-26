# T100 → MEXC real-money execution: blocked until certified

**As of 2026-10-23:** This branch introduces a mode boundary and a *read-only*
MEXC account preflight. It does not create a live execution path or send an order.
Do not merge or change Render variables expecting trading to start.

## Why toggling the current T100 trader to live is unsafe

`app/t100_200_trader.py` currently consumes Stage-2 signals, creates simulated
positions in `t100_positions`, deducts *simulated* fees/slippage and closes
positions based on completed Min30 OHLC bars. It does not submit MEXC orders,
reconcile real fills, or require exchange-native stop protection.

The V40 **VOL30_SKIP+H4** results were produced by a separate *historical*
replay. Live T100 production currently lacks the complete frozen V40
admission/sizing bridge and a production-faithful 100/200-bps execution-cost
mapping. H4 is a four-bar stressed **exit-capacity** horizon in the research
model, not a 120-minute entry child-order window. Simply routing Stage-2 to
MEXC would NOT replicate the studied portfolio.

## What this branch does

- `T100_EXECUTION_MODE=paper` remains the default: existing paper path unchanged.
- `T100_EXECUTION_MODE=live` fails closed before the trader connects to the
  production database, even if the operator has set all arming flags.
  **LIVE_ADAPTER_STATUS is NOT_IMPLEMENTED.**
- `python -m app.t100_live_preflight` performs only public and signed *GET*
  calls for account readiness; it does not test placing futures orders.
- Operator risk controls are defined and validated but do not authorize trades.

## Explicit future live risk controls (currently ineffective as a live switch)

```
T100_EXECUTION_MODE=paper
T100_LIVE_ORDER_API_ENABLED=false
T100_LIVE_KILL_SWITCH=true
T100_LIVE_LEVERAGE=1
# When a production live execution adapter is independently ready:
# T100_LIVE_OPERATOR_CONFIRM=I_UNDERSTAND_MEXC_REAL_FUNDS_AT_RISK
# T100_LIVE_MAX_POSITION_NOTIONAL_USDT=<positive decimal>
# T100_LIVE_MAX_TOTAL_NOTIONAL_USDT=<positive decimal>
# T100_LIVE_MAX_DAILY_LOSS_USDT=<positive decimal>
```

Never store API keys in source, logs or GitHub. Prefer a separate exchange
subaccount with futures-only API permission, IP allowlisting and withdrawal
permission disabled. MEXC requires futures KYC/API permission and operates
against the real exchange (no sandbox).

## Required implementation and certification *before* switching

1. Preserve frozen V40 signal filtering/sizing chronology and test entry/exit
   parity against the historical $2k benchmark and delayed-fill cases.
2. Use a **separate live order ledger, live run ID, and exchange-account inventory**;
   do not write paper positions as though they were exchange fills.
3. Implement MEXC authenticated order submission with persistent externalOid
   and crash/retry-safe idempotency. A timed-out POST is UNKNOWN, not FAILED:
   query by externalOid and reconcile before ever attempting a retry.
4. Enforce symbol allowlists, available margin, max contracts, volume steps,
   tick precision, exchange API permission and prohibited pairs at order time.
5. Before/alongside each exposure, verify an exchange-side protective stop or
   explicit fail-closed emergency procedure for stop placement failure; use
   actual filled contracts and hedge/one-way position mode correctly.
6. Preserve the frozen V40 5%-of-prior-30min-turnover admission/size cap and
   H4 stressed exit-capacity logic. Live execution must separately use a
   **fresh** Level-2 book, adverse reference-slippage checks, partial-fill
   handling, fees, funding, dynamic free margin and account exposure. Do not
   reinterpret H4 as a 120-minute entry execution window, and do not use
   same-candle fantasy fills.
7. Implement full exchange/order/position/stop reconciliation on startup and
   each cycle, leader fencing across Render rolling deployments, circuit
   breakers, daily-loss limits and operator kill-switch behavior.
8. Validate order-free sandbox substitutes via mocked authenticated responses;
   MEXC itself has no futures sandbox. Complete a supervised,
   explicitly authorized, tiny real-money test only after the above safeguards.
9. Verify trading API eligibility in the account's jurisdiction. Approval is
   account-specific. Use the current documented endpoint host
   `https://api.mexc.com`, not legacy `contract.mexc.com`.
10. Avoid auto-deploy to live from an unreviewed commit. Preserve existing
    paper service and consider a separate Render worker for live execution.

## Why an exchange-native stop is essential

Historical Min30 stop/trailing exits are model outcomes, not executable
instructions. Waiting for the next completed bar may expose a live
perpetual position to larger losses or liquidation, particularly in thin
books, gaps or exchange outages. Trailing parameter semantics and fill
priority can differ from the historical simulation.

## Read-only preflight

In a trusted local environment with project dependencies installed:

```bash
python -m app.t100_live_preflight
python -m pytest tests/test_t100_live_guards.py -q
```

Read-only private calls require `MEXC_API_KEY` and `MEXC_API_SECRET` from
the environment; do not put them in your terminal transcript or chat.

Output should always say `live_order_submission_possible: false`.

## Sources
- MEXC futures API official docs:
  https://www.mexc.com/api-docs/futures/integration-guide
- Official place order:
  https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/place-order
- MEXC launch:
  https://www.mexc.com/announcements/article/introducing-api-futures-trading-on-mar-31-2026-17827791534551

# T100 full-V40 live strategy target — operator preference (2026-10-27)

**Planning target only. Live execution remains blocked, shadow remains
paper/GET-only, no trade authorization or new API permissions.**

The operator explicitly prefers the actual V40 strategy's dynamic sizing,
admission and trailing exit policy for eventual autonomous real trading,
not permanent conservative/pilot caps. A downsized execution test must be
labelled NONPARITY if one is required to certify the exchange mechanics;
never silently represent that test as full V40.

## Frozen V40 model that must be preserved

From `app/t100_v40_admission.py`:
- four simultaneous position slots, one position per symbol;
- causal 49 consecutive prior 30-minute candles;
- VOL30 skip if prior-24h volatility >= 30%;
- ATR candidate floor 4%, LOW tier below 5.7%, HIGH at/above 5.7%;
- LOW gross target 100% of account equity / 4 = **25% per slot**;
- HIGH gross target 250% / 4 = **62.5% per slot**;
- THEN clip modeled notional by 5%-prior-turnover, P25, stressed
  H4 **exit** capacity, same-bar used capacity and the historical
  pooled-margin shock budget;
- 5x in shock_budget is a **historical risk assumption**, NOT a
  separately certified live margin or leverage setting;
- H4 four bars are for stressed EXIT sizing, not a four-bar live
  order-entry schedule.

A 2,000 USDT account illustrates the *before clipping* targets:
LOW 500 USDT/slot, HIGH 1,250 USDT/slot; four concurrent HIGH slots
can target 5,000 USDT gross notional (250% of equity), before caps.
This is **not** a promise that the exchange can safely fill/hold
those amounts or that this position size is feasible with 1x leverage.

Frozen trailing policy: activate around 14% profit, 0.5 percentage-point
trailing gap, initial historical SL 75%; must verify exact stop trigger,
fill semantics, fee/funding model and inability to guarantee stop fills.

## Hard promotion gates, not optional exposure restrictions

- Full point-in-time V40 strategy parity including entry, slot occupancy,
  contract rounding, stop/exit chronology, fees and funding against the
  pinned historical archive (not just one admission formula).
- Execute with observed current MEXC contract specs, L2, price slippage,
  margin constraints, per-symbol lot sizes, and position/stops count.
- Decide/verify leverage and liquidation budget for real notional and
  actual account equity. Never translate 5x historical proxy into 5x
  live leverage without an independently audited risk model.
- Native protective stops, incident recovery and durable accounting,
  verified under actual exchange state; no operator-free fallback
  after ambiguous order submissions.
- Independent review and separately explicit full-account risk approval.
- A historic 8x (or other multiple) is a backtest observation, not a
  certified or guaranteed live return.

The current `LiveMutationPermitIssuer` is still hard-blocked by
`LIVE_ADAPTER_STATUS=NOT_IMPLEMENTED`. No automation may enable full
exposure simply by changing environment variables or applying DB DDL.

## DB-specific requirements

The full strategy database inspection must include:
- 027 live order-intent state machine
- 028 VOL30 parent/child research order state
- 029 checkpoints, stop amendments, fill/funding/accounting ledgers
- 030 monotonically fenced executor lease

The dedicated live role should have column-scoped UPDATE rights only on
mutable VOL30/lease fields, and no DELETE/TRUNCATE on any live tables.
The 027–030 DDL is NOT confirmed applied at time of writing.

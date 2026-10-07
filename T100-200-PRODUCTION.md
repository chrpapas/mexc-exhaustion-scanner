# T100_200 production promotion

Strategy ID: `t100_200_stage2_p15_a4_sl75_trail10_gap1_v1`

This release promotes the frozen T100_200 strategy to the production **paper/shadow**
service. It does not enable live MEXC orders.

## Frozen signal contract

- Native Min30 P2 lifecycle.
- Daily Core + Persistence V2 remain inside P2 admission.
- Physical episode Stage1 = first P2, Stage2 = second P2.
- Stage3+ never enter.
- A new physical episode begins when an exact-clock Min30 rolling 72h return is
  non-positive strictly between the previous and current P2 feature timestamps.
- Stage1 gate only: causal strict365 history, r24 >= 15%, ATR7 >= 4%.
- No ATH/destruction gate.

## Frozen sizing

Four slots, one position per symbol.

- Stage1 ATR7 4.0% to <5.7%: LOW_100, entry notional = 25% of current equity.
- Stage1 ATR7 >=5.7%: HIGH_200, entry notional = 50% of current equity.

This reproduces 100% / 200% gross tier exposure across four slots. Capacity is
still four simultaneous names; the HIGH tier is larger notional, not extra slots.

## Frozen exits and paper costs

- SL75 before trailing is active.
- No fixed take-profit.
- Trail arms at +10% short profit.
- Trail floor follows best profit minus 1 percentage point.
- ADVERSE_FIRST completed-bar ordering.
- Fee: 0.08% per fill.
- Slippage: 25 bps each side as a pure P&L debit; trigger prices are unchanged.
- Historical/current MEXC funding is applied to the paper position where available.

## Service wiring

`mexc-exhaustion-scanner` continues the previous Min15 scanner only as a muted
research collector. Its old subscriber/performance output and legacy-trader watchdog
are disabled.

`mexc-standard-short-trader` now starts `python -m app.t100_200_worker`.
The worker hard-fails unless `TRADING_MODE=paper` and contains no live-order code.

The T100 worker persists its own scanner lifecycle, physical stages, P2 signals,
portfolio positions, funding applications, and events in `t100_*` tables so the
old research/trader tables remain intact for auditability.

## Rollback

Restore the prior Render start commands from the previous commit. The new tables
are additive and do not modify legacy scanner/trader rows.

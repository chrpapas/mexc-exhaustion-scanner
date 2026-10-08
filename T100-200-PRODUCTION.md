# T100_225 production promotion

Strategy ID: `t100_225_stage2_p15_a4_sl75_trail14_gap0p5_v3`

This release runs the certified A14/G0.5 exit geometry with T100_225 sizing in the production **paper/shadow**
service. It does not enable live MEXC orders. Signal generation, Stage1 gate, slot count,
SL75, fees, slippage, and funding treatment remain unchanged.

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
- Stage1 ATR7 >=5.7%: HIGH_225, entry notional = 56.25% of current equity.

This reproduces 100% / 225% gross tier exposure across four slots. Capacity is
still four simultaneous names; the HIGH tier is larger notional, not extra slots.

## Frozen exits and paper costs

- SL75 before trailing is active.
- No fixed take-profit.
- Trail arms at +14% short profit.
- Trail floor follows best profit minus 0.5 percentage points.
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


## v1.5.0 A14/G0.5 promotion

A14/G0.5 was selected after the locked trail-confirmation and robustness certification
passes. Under the normal certified cost model it produced 212.0% CAGR with -58.0%
trigger-aware drawdown over the full historical replay. It also beat the previous
A10/G1 production control under every covered-window stress scenario tested,
including 50 bp slippage each side plus 1.5x negative funding.

Cutover semantics are intentionally non-retroactive: any position opened before the
promotion keeps the trail activation/gap stored on that position (10% / 1pp for the
prior strategy). New positions opened after promotion are stamped 14% / 0.5pp.


## v1.6.0 T100_225 sizing promotion

The certified A14/G0.5 exit geometry remains frozen. This promotion changes only
the HIGH-tier paper notional from 50% to 56.25% of current equity, equivalent to
LOW100/HIGH225 gross tier sizing across four slots.

Certified BASE25 anchors:
- Full history: $10,000 -> $932,552; CAGR 247.4%; trigger-aware DD -61.0%; CAGR/DD 4.06.
- Fully covered MEXC funding window: $10,000 -> $198,787; CAGR 676.7%;
  trigger-aware DD -61.0%; CAGR/DD 11.10.
- Zero liquidation-breach bars across all full-history stress cases.
- Candidate beat T100_200 on return and CAGR/DD in every covered stress case.

Cutover is non-retroactive. Existing open positions keep their fixed original
notional. Legacy pending HIGH_200 signals remain supported at 50%; newly generated
HIGH_225 signals use 56.25%. Production remains paper-only.

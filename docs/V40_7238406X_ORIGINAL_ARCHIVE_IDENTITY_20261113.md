# Frozen V40 7.238406× source of truth — 13 November 2026

**Status: exact original archived identity verified; real execution NOT released.**

The original `v40_parity_for_live.zip` was recovered from the operator's
owned ChatGPT file library, and its byte-for-byte SHA-256 verified as:

`2918f4589e8d564ca55b6f24aa2e9a868b5d214653ad7d92acaf1729fd1e4cb5`

The private sample ZIP is **not** committed to GitHub; only aggregate
diagnostics and the manifest identity are retained.

## Exactly which 7.24× backtest

- Archive scenario: `V32_VOL30_SKIP_H4_S100_CAP5_V39_HYBRID`.
- $2,000 starting equity, **$14,476.81238416 final MARKED equity**,
  7.238406× saved multiple.
- 108 entries, 105 completed exits, **3 open positions at the endpoint**.
- Peak **actual gross notional / marked account equity = 2.0901×**.
  The earlier theoretical 250% was a four-HIGH-target MAX, not the
  historical peak actually reached.
- High-envelope drawdown −54.8664%; close-mark drawdown −50.0955%.
- Original nominal pooled-initial-margin leverage proxy **5×**,
  reserve 5%, simultaneous adverse shock 20%.
  Historical 5× is NOT proof the actual MEXC API should set `leverage=5`;
  this is a backtest risk assumption not actual venue evidence.
- Stage2 146 candidate signals, VOL30 skip, four slots, low target
  25% / high target 62.5% of current equity per slot.
- Short initial SL75, trailing activation +14%, trailing gap 0.5pp,
  **adverse-first** Min30 OHLC with `open_worst` gap exits: model SHORT
  exit price `max(stop_trigger, candle_open)` when high breaches an
  already-active stop. Price after a real exchange stop is a *signed fill*,
  NOT this offline model price.
- Prior turnover 5% entry-and-exit participation, P25/P10 liquidity,
  H4 stressed four-bar exit capacity, V39.1 integer-lot rounding.
- 23 liquidity-constrained partial exits, 16 delayed exits, up to 3
  completed Min30 bars of exit delay; 45 simulated extra same-bar/same-price
  child orders.
- **10 HYBRID entry passthroughs and 13 exit-fill passthroughs across nine
  unverified contract-spec symbols**. Historical mark/maintenance tiers,
  venue order book depth, native SL fill behavior and actual MEXC
  liquidation could not be reconstructed or certified.
- Funding and trading fees occurred in the saved simulation; the older
  audit independently certified *entry/admission arithmetic* but did not
  independently reconstruct the full portfolio + funding + exit path.

## Present live mismatch

The current Render worker still runs
`app.t100_v40_render_executor_disabled_worker` in paper/GET-only
mode; no live four-slot executor is connected. A one-short, CROSS 1×
$12.50 limit pilot is **NOT** identical to this backtest.

The frozen replay had **209% of equity in gross shorts at peak**, so 1×
initial margin would not support the historical position exposure. The
backtest's 5× initial-margin proxy is NOT proof that a particular MEXC
leverage setting and fair-price liquidation tiers would have survived.
The actual exchange risk and native-stop lifecycle must be reconstructed
under observed per-symbol specifications and signed account bookends.
A stop loss 75% above entry can be reached after liquidation if account
collateral is insufficient; liquidations must never be modeled as
ordinary SL exits.

The MEXC API supports native order placement and conditional stops, but
actual trigger/fill execution, native SL amendment and hidden spread/
slippage must be verified separately. Live exits may differ from the
historical delayed-exit turnover model, even with identical decisions.

## Code locked by this change

- `app/t100_v40_frozen_replay_identity.py`: independently checks a local
  original ZIP, exact SHA, scenario and exported metrics, without uploading
  symbols, trades, API secrets or account data.
- `app/t100_v40_unreleased_exit_policy.py`: model stop-hit decision now
  matches V39.1's conservative gap-open `open_worst` rule.
- Deterministic offline tests. These modules never send orders and are not
  imported by current Render worker.

## Deployment criterion

**Do not claim the 7.238406× strategy is running** until a four-slot live
executor is wired, exact frozen historical admission/exit/margin mechanics
are reproduced with actual current contract-lot and risk rules, and
exchange-signed native protection and exits survive real restart/failure
tests. A genuine Stage2 signal, not a forced trade, is always required.

Do not clear the independent guardian's new 18:42:05 Zurich signed
hedge-mode GET incident by updating a latch row; any recovery must verify
account/ledger, preserve historical incident outbox, and certify new
guardian state before an order can be eligible.

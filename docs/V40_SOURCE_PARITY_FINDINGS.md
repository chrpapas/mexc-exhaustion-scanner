# V40 source-grounded parity findings (2026-10-23)

User-uploaded original research modules have been inspected:
`contract_replay_v40_scale.py`, `contract_replay_v39.py`,
`combined_v32.py`, `exit_horizon_v31.py`, `volatility_v28.py`,
`preentry_v26.py`, `ablation_v15.py`, `research_shock.py`.

**This file corrects material assumptions in the earlier VOL30/H4 V2
prototype. LIVE TRADING IS BLOCKED; DO NOT MERGE/DEPLOY AS LIVE WORKER.**

1. **H4 is NOT a 120-minute entry-scheduling window.** It is the number of
   future 30m bars in a *stressed exit-capacity sizing assumption*:
   `exit_bound = 0.05 * past24h_P10_30m_turnover * 4 * 0.50 / 1.75`.
   Historical entries occur at their admission candle. V39 current-lot
   child splits are same-candle, same-price mathematical splits.
   The previously shared planner that accumulates entries over two hours
   defines a DIFFERENT strategy, not the tested V40 policy.
2. **V40 HIGH targets 62.5% of equity before constraints**, rather than
   the current paper T100 HIGH_225 target of 56.25%. V40 specs are
   `high_gross=250%`, `low_gross=100%`, four slots, hence
   `250%/4=62.5%` and `100%/4=25%`.
3. **VOL30_SKIP is a volatility filter**: exclude candidates with
   `pre_24h_close_vol_pct >= 30`. The feature uses strictly prior 49
   consecutive Min30 closes, 48 log returns, sample standard deviation
   multiplied by `sqrt(48)*100`, rounded to six decimals.
4. **Causal turnover capacity** requires 48 positive prior Min30 quote-
   turnover observations. Entry notional is constrained by
   `min(5% previous30m, 5% previous24h P25, H4 exit_bound)` as well as
   same-bar quota and a pooled equity/margin shock budget.
5. **V40 risk-budget research assumes 5x nominal leverage and a 20%
   upward-price shock** with 5% equity reserve and entry costs.
   Existing draft isolated 1x live gateway is NOT equivalent: the
   historical 7.238x / 4.421x / 1.977x results are not live predictions.
6. Preentry spot-rate/volume consistency checks and all remaining
   position-management, stop/trail, funding and true fill behavior still
   need end-to-end review. No historical or current L2 fill certification.

## Implemented in this PR

`app/t100_v40_admission.py` provides a pure, order-free port of V40
volatility gate, lower-order-statistic turnover profile, stressed H4 exit
capacity, 5x *historical* shock-budget formula and V39 lot rounding.

Local isolated package tests passed 57/57; a separate differential
harness directly invoked the provided original research source functions
and passed 1,000 randomized admission scenarios + 150 lot splits.

These are **component tests only**. Full parity requires the original
historical SQLite, exact Stage-2 candidate rows and account-path/funding
replay; it was not run. Exchange requests and Render services are unchanged.

## Do not operationalize until

- Signal timing/eligibility matches V40 candidate generation (146 baseline).
- Exact $2k/$10k/$100k chronological admission and exit parity is measured.
- Live execution differs conservatively only where explicitly documented;
  full-size immediate fills must not be assumed.
- Native exchange protective stops, continuous trailing, cancellation,
  timeouts, partial fills and broker restart state are integrated/tested.
- Out-of-sample and real venue permission/availability is established.

**T100_EXECUTION_MODE=live remains hard-blocked by design.**

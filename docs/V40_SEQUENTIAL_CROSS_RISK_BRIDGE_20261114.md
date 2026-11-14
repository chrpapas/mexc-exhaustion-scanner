# V40 sequential-lot to CROSS stress bridge (unreleased)

The V40 frozen backtest assumed an internal 5x pooled shock-budget
proxy; it did not establish actual MEXC leverage, risk-tier rules
or liquidation paths. At historically targeted four HIGH slots, a joint
SL75 adverse move can cost substantially more than full account equity,
even where nominal initial margin fits.

## What the new bridge does

`app/t100_v40_unreleased_sequential_lot_margin.py` now returns
the exact nonauthorizing per-symbol V39 lot-floor plans constructed from
the already sequentially shock/turnover-clipped V40 admissions.

`app/t100_v40_unreleased_sequential_cross_risk.py` accepts:
- those exact frozen plans;
- independently sourced full-account economic evidence via
  `CrossAccount`;
- one independently sourced `ShortCrossPosition` per identical symbol
  with exact contract quantity, lot size, observed leverage, fair price,
  and verified stressed-tier MMR upper bound.

It compares modelled entry notionals, symbol sets, quantities and
leverage, then calls the existing conservative MEXC margin calculator
for +20% adverse fair-price research shock, +75% SL75 shock and
+80% SL75-plus-5-point gap scenario.

It intentionally rejects unverified stressed maintenance tiers and
account inventory. No fixed 5% MMR is silently substituted.
All scenario outputs remain **offline estimates, not actual liquidation
guarantees**, because MEXC can update tiers and liquidate along a price
path. Actual market execution can gap beyond a protective stop.

## What is NOT completed

- Connecting the pure model to *current* MEXC private signed GET, public
  contract metadata, full-account available collateral and future-tier
  proof in an independently authorized live execution path.
- An exact full exit/funding/partial-fill replay of the original
  7.238406× frozen V40 HYBRID historical scenario.
- Certified live four-slot reservations and stop execution, durable
  native trailing-stop amendment readbacks and independently verified
  slot release.
- Guardian incident recovery (active scope 003 latch).
- User authorizing a newly reviewed four-slot risk envelope distinct
  from the current one-position $12.50 canary.

No order can be submitted from this code, no latch or exchange setting
is touched, and no report sets live_authorization true.

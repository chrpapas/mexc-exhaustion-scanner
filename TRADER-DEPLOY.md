# Trader deployment — v1.3.52

v1.3.52 promotes the frozen **First-Entry Trend Persistence V1** veto into trader/subscriber admission. The underlying exhaustion confirmation, TP5/SL75 exits, sizing and capacity remain unchanged.

## Current promoted strategy

`tp5_sl75_daily_core_persistence_skip_v1`

A confirmed STANDARD or HIGH_RISK short is admitted only if it passes **both** hard filters:

1. **Daily-Confirmed Core V1** must be false.
2. **First-Entry Trend Persistence V1** must be false.

Daily-Core V1 remains:

```text
Continuation Core V1:
run_score >= 5
AND distance_above_ema20_atr_4h >= 3
AND (previous_momentum_1h > 0 OR cross_section_percentile >= 0.99)

AND

Daily Bull V1:
last completed Day1 close > EMA20D
AND EMA20D slope > 0
AND 3D momentum > 0
```

Persistence V1 is evaluated only after Daily-Core admits. It hard-skips when:

```text
Daily Bull V1 = true
AND Continuation Core V1 = false
AND daily distance above EMA20D >= 4.5 ATR
AND one-day EMA20D slope >= 7.5%
AND run -> breakdown <= 6h
```

The thresholds are exactly the v1.3.51 frozen rule; promotion does not retune them.

## Execution remains unchanged

- STANDARD + HIGH_RISK only
- fixed 5% of current equity per admitted position
- 6 generic slots
- 30% max aggregate exposure
- one open position per symbol
- 1x cross
- TP +5%
- catastrophic SL -75%
- no time expiry

## Render values

Scanner service:

```text
SUBSCRIBER_SIGNAL_STRATEGY=tp5_sl75_daily_core_persistence_skip_v1
```

Trader service:

```text
TRADER_EXECUTION_STRATEGY=tp5_sl75_daily_core_persistence_skip_v1
TRADER_PAPER_RUN_ID=tp5_sl75_daily_core_skip_v1
TRADER_ALLOWED_RISK_TIERS=STANDARD,HIGH_RISK
TRADER_MAX_OPEN_POSITIONS=6
TRADER_SLOT_ALLOCATION_PCT=5
TRADER_MAX_TOTAL_EXPOSURE_PCT=30
TRADER_TP5_TARGET_PCT=5
TRADER_CATASTROPHIC_STOP_PCT=75
TRADER_ALLOW_SAME_SYMBOL_PARALLEL=false
TRADER_MARGIN_MODE=cross
TRADER_LEVERAGE=1
TRADER_PROCESS_EXISTING_SIGNALS=false
TRADER_MAX_SIGNAL_AGE_SECONDS=900
```

### Important: keep the current paper run ID

Do **not** change `TRADER_PAPER_RUN_ID` during this promotion. Keeping:

```text
TRADER_PAPER_RUN_ID=tp5_sl75_daily_core_skip_v1
```

prevents the paper-run reset logic from closing the currently open positions and resetting equity. Existing ARB/USELESS/etc. positions keep their persisted TP5/SL75 management. Persistence V1 affects only future confirmed signals.

For a later clean A/B experiment you may deliberately start a new run ID, but doing that archives/closes the old paper book by design.

## Fail-closed decisions

The trader records distinct decisions:

```text
ignored_daily_core_filter
ignored_missing_daily_core_data
ignored_daily_bull_persistence_filter
ignored_missing_persistence_data
```

Migration `018_daily_bull_persistence_trader_decisions.sql` extends the PostgreSQL CHECK constraint to allow the two new Persistence V1 decisions. It is applied by the normal migration runner.

The scanner embeds `hours_run_to_breakdown` in new confirmed signals. The trader also derives it from `pump_episodes.started_at` and `pump_episodes.breakdown_at` if necessary, making scanner/trader rolling deploy order safe.

## Research integrity

Persistence V1 was designed after reviewing USELESS/PONS. Its original research freeze stays:

```text
03 Sep 2026 20:53 CEST
```

Promotion does **not** rewrite that boundary. All earlier results remain retrospective calibration. The existing Research Intelligence card continues tracking the untouched post-freeze cohort.

## Rollback

Restore plain Daily-Core only:

```text
TRADER_EXECUTION_STRATEGY=tp5_sl75_daily_core_skip_v1
SUBSCRIBER_SIGNAL_STRATEGY=tp5_sl75_daily_core_skip_v1
```

PCR remains available as the older rollback:

```text
TRADER_EXECUTION_STRATEGY=tp5_sl75_pcr_v1
```

## Verification after deploy

Run:

```bash
python -m app.trader_status
python -m app.report_now
python -m app.research_analytics_now
```

`trader_status` should show:

```text
Strategy: tp5_sl75_daily_core_persistence_skip_v1
```

A future persistence-flagged confirmation should appear in trader logs as:

```text
decision=IGNORED_DAILY_BULL_PERSISTENCE_FILTER
```

while a safe signal continues to open at 5% current equity subject to the same slot, symbol and 30% exposure limits.

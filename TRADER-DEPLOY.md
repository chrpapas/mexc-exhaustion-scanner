# Trader deployment — v1.3.56

## v1.3.56 strategy promotion

This release promotes **Trend Persistence V2** after the 8 Sep 2026 SOPH review. It changes future signal admission only; TP5, SL75, sizing and slot logic are unchanged.

Current strategy:
`tp5_sl75_daily_core_persistence_skip_v2`

Admission order:
1. Daily-Confirmed Core V1 hard skip (fail closed).
2. Trend Persistence V2 hard skip (fail closed on the reachable branch).
3. Existing symbol/capacity/exposure checks.

Persistence V2 is the OR of two frozen branches:

```text
EARLY V1
Daily Bull=true
AND Continuation Core=false
AND daily distance >= 4.5 ATR
AND daily EMA20 1D slope >= 7.5%
AND run -> breakdown <= 6h
```

```text
MATURE-RUN WEAK-BREAKDOWN V1
Daily Bull=true
AND Continuation Core=false
AND run -> breakdown >= 24h
AND previous 1h momentum > 0
AND lower_high_and_close = false
AND structural_break_15m = false
```

The mature-run branch is frozen **08 Sep 2026 08:29 CEST**. Do not retune this threshold set in place; any change becomes a new version/freeze.

## Execution unchanged

- STANDARD + HIGH_RISK only
- fixed 5% current equity per admitted position
- 6 generic slots
- 30% max aggregate exposure
- one open position per symbol
- 1x cross
- TP +5%
- catastrophic SL -75%
- no time expiry

## Render values

Scanner:
```text
SUBSCRIBER_SIGNAL_STRATEGY=tp5_sl75_daily_core_persistence_skip_v2
```

Trader:
```text
TRADER_EXECUTION_STRATEGY=tp5_sl75_daily_core_persistence_skip_v2
TRADER_PAPER_RUN_ID=tp5_sl75_daily_core_persistence_skip_v2
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

Using the new paper-run ID gives a clean V2 forward book. Historical V1 positions/results remain archived rather than being counted as V2 entries.

Migration `020_persistence_v2_trader_decisions.sql` is automatic and adds the distinct decision `ignored_mature_run_weak_breakdown_filter`.

## Post-deploy checks

```bash
python -m app.trader_status
python -m app.report_now
python -m app.research_analytics_now
```

Expected research Discord wording: **Daily-Core + Persistence V2**, with true-forward evidence frozen at **08 Sep 2026 08:29 CEST**.

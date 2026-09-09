# Trader deployment — v1.3.57

## v1.3.57 opportunity recall + allocation

Scanner lifecycle changes:
- `ARMED_RUNNER_MEMORY_HOURS=48`: unconfirmed runner episodes retain prior pump qualification for 48h after their latest tracked peak/detection.
- `CONFIRMED_REARM_HOURS=48`: after 48h, a previously confirmed symbol may create a fresh episode when it independently qualifies again; the existing +5% new-high re-arm remains active.
- The worker no longer overrides the classifier's intended late-prior-runner exception with a second raw run-score gate.
- Entry confirmation remains exhaustion -> structural break -> failed retest -> Daily-Core/Persistence V2 admission.

Trader sizing changes for **new entries only**:
- 6 generic slots
- 8.333333333333% current equity per slot
- 50% max aggregate exposure
- 5 STANDARD slots + 1 HIGH_RISK slot
- TP +5%, catastrophic SL -75%, 1x cross unchanged

Existing open positions are not resized. Keep the current paper-run ID if you do not want deployment to archive/reset the current paper book.

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
ARMED_RUNNER_MEMORY_HOURS=48
CONFIRMED_REARM_HOURS=48
REARM_NEW_HIGH_PCT=0.05
EPISODE_MAX_AGE_HOURS=240
```

Trader:
```text
TRADER_EXECUTION_STRATEGY=tp5_sl75_daily_core_persistence_skip_v2
TRADER_PAPER_RUN_ID=tp5_sl75_daily_core_persistence_skip_v2
TRADER_ALLOWED_RISK_TIERS=STANDARD,HIGH_RISK
TRADER_MAX_OPEN_POSITIONS=6
TRADER_SLOT_ALLOCATION_PCT=8.333333333333
TRADER_MAX_TOTAL_EXPOSURE_PCT=50
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

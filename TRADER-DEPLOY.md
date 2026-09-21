# Trader deployment — v1.3.82

## v1.3.81 current paper-production strategy — ATR Hard Filter V1

`tp5_nostop_adv30_runner50_trail1_daily_core_persistence_atr_hard_v1`

Final admission after Daily-Core + Persistence V2: `atr_15m_pct >= 0.02461` on every signal. Missing ATR data fails closed. The trader repeats the same check before entry. Paper execution remains 10×10% MTM / 100% cap with the adverse-30 recovery runner.

Rollback model strategy: `tp5_nostop_adv30_runner50_trail1_daily_core_persistence_atr_gate_v1`.



## v1.3.80 rollback candidate — ATR Capacity Gate V1

`tp5_nostop_adv30_runner50_trail1_daily_core_persistence_atr_gate_v1`

The scanner remains Daily-Core + Persistence V2 fail-closed. The trader keeps the existing 10×10% MTM / 100% exposure / no-stop adverse-30 runner execution and adds a scarcity-only capacity gate:

- occupancy 0–6: admit normally;
- occupancy 7–9: require frozen confirmation-time `atr_15m_pct = atr_15m / retest_close >= 0.02461`;
- signals in the same 5-minute confirmation bucket are ranked by descending `atr_15m_pct`;
- missing ATR or entry price is fail-closed when the gate is active;
- the legacy no-gate runner remains selectable as `tp5_nostop_adv30_runner50_trail1_daily_core_persistence_skip_v2`.

Render defaults use a new paper run id so the gated candidate is measured separately from the prior runner.


## v1.3.75 promoted production candidate

Promoted trader strategy:
`tp5_nostop_adv30_runner50_trail1_daily_core_persistence_skip_v2`

Frozen portfolio parameters for new positions:
- Daily-Core + Persistence V2 admission, fail closed
- STANDARD + HIGH_RISK
- 10 generic slots
- 10% of current MTM equity per slot
- 100% max nominal exposure
- one open position per symbol
- 1x cross
- no stop-loss before TP5
- ordinary trades: full close at +5%
- recovery-runner trades: if max adverse return reached at least -30% before first TP5, realize 50% at TP5 and keep 50% in the same slot with a 1 return-percentage-point trailing profit floor
- runner stop updates in 0.25 percentage-point steps; a too-small MEXC contract that cannot be split safely falls back to a full TP5 close

Existing open positions retain their persisted exit strategy and are not mutated by deployment.
Migration `021_recovery_runner_exit_strategy.sql` only extends the persisted exit-strategy constraint. Partial TP5 P/L and fees are booked on the same position row so paper cash reconstruction remains idempotent.

Render strategy values:
```text
TRADER_EXECUTION_STRATEGY=tp5_nostop_adv30_runner50_trail1_daily_core_persistence_skip_v2
TRADER_PAPER_RUN_ID=tp5_adv30_runner50_trail1_candidate_v1
TRADER_ALLOWED_RISK_TIERS=STANDARD,HIGH_RISK
TRADER_MAX_OPEN_POSITIONS=10
TRADER_SLOT_ALLOCATION_PCT=10
TRADER_MAX_TOTAL_EXPOSURE_PCT=100
TRADER_TP5_TARGET_PCT=5
TRADER_ALLOW_SAME_SYMBOL_PARALLEL=false
TRADER_MARGIN_MODE=cross
TRADER_LEVERAGE=1
TRADER_PROCESS_EXISTING_SIGNALS=false
TRADER_MAX_SIGNAL_AGE_SECONDS=900
```

The repository deliberately keeps `TRADING_MODE=paper` and `MEXC_LIVE_ORDER_API_ENABLED=false` in `render.yaml`. Live execution remains fail-closed and must still be explicitly armed with valid Futures credentials, `TRADING_MODE=live`, `MEXC_LIVE_ORDER_API_ENABLED=true`, and `LIVE_TRADING_CONFIRM=I_UNDERSTAND_LIVE_TRADING`.

## v1.3.60 rolling-deploy concurrency fix

Confirmed-signal consumption is guarded by a non-blocking PostgreSQL advisory lock. During Render overlap only one trader process may recover or consume signals; other instances skip that consumption tick and retry later. This eliminates the duplicate `trader_positions_signal_id_key` errors seen with orphan recovery while preserving the existing paper run ID and positions. No migration is required. Paper equity adjustments are atomic, entry fees are booked only after a position row is created, and startup rebuilds realized paper cash from the run ledger to repair any extra fee debit caused by the prior race.


## v1.3.59 opportunity recall + allocation

### Restart catch-up semantics
On a paper-run switch, v1.3.59 preserves the prior trader cursor. Signals emitted by the scanner during the trader restart are consumed after startup and then pass the normal 15-minute freshness/admission checks. The trader no longer jumps its cursor to the latest confirmed signal during a run reset. It also checks the last `TRADER_MAX_SIGNAL_AGE_SECONDS` window for confirmed shorts with no trader decision and no position, recovering them exactly once.


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
SUBSCRIBER_SIGNAL_STRATEGY=tp5_nostop_adv30_runner50_trail1_daily_core_persistence_skip_v2
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


Current v1.3.59 paper run ID: tp5_sl75_persist_v2_armed48_50pct_v1


### v1.3.65 promoted strategy
`TRADER_EXECUTION_STRATEGY=tp5_sl100_lae10_24_q1_daily_core_persistence_skip_v2`

`TRADER_CATASTROPHIC_STOP_PCT=100`

Existing open positions retain persisted legacy exit metadata/protection. New positions use SL100 + LAE10/24-Q1.


### v1.3.72 historical-universe research
Do not run the universe reconstruction while the candle collector is active; both commands intentionally share the same lock and the second command will refuse to start. After the current candle fetch completes, run the universe reconstruction, then seed a follow-up candle fetch with `research-history/universe-history/historical-seed-symbols.txt`. This research path does not access the production database or change live strategy/trader settings.


### v1.3.72 one-shot historical research
This remains isolated from the live DB/trader. Start it once with:

```bash
python -m app.historical_pipeline run --cache-dir ./research-history-v2 --months 6
```

The controller freezes the time window, checkpoints every 15 minutes, automatically
continues until current candles are complete, reconstructs delisted/historical symbols,
then downloads their missing candles and exits. If interrupted, re-run the same command.
## Historical live-schema refetch (v1.3.76)

The new historical fetcher is offline research only and does not place orders or require MEXC credentials. Run it in a separate local process/service from the production worker. See `README.md` for `app.historical_live_store` and `app.historical_live_validate` commands. Do not promote six-month replay results unless the production-overlap validation gate passes.



## v1.3.79 alignment

Use the same canonical strategy id on scanner and trader:

```text
SUBSCRIBER_SIGNAL_STRATEGY=tp5_nostop_adv30_runner50_trail1_daily_core_persistence_skip_v2
TRADER_EXECUTION_STRATEGY=tp5_nostop_adv30_runner50_trail1_daily_core_persistence_skip_v2
```

The old scanner `tp5_sl75_daily_core_persistence_skip_v2` value is still accepted as a compatibility alias, but should no longer be used for new deployments. The subscriber August replay is fail-closed against the frozen 473/366 reference universe.

## v1.3.78 reporting

`python -m app.report_now` now sends the subscriber three-layer board: actual active trader run, current production strategy replay since the retained August data begins, and the capacity-independent arithmetic sum of all eligible current-strategy signals. `python -m app.research_analytics_now` is research-diagnostics only and no longer uploads legacy strategy-comparison CSVs.


## ATR Capacity Gate V1 (v1.3.80)

Production candidate settings:

```text
TRADER_EXECUTION_STRATEGY=tp5_nostop_adv30_runner50_trail1_daily_core_persistence_atr_gate_v1
TRADER_MAX_OPEN_POSITIONS=10
TRADER_SLOT_ALLOCATION_PCT=10
TRADER_MAX_TOTAL_EXPOSURE_PCT=100
ATR_CAPACITY_GATE_ENABLED=true
ATR_CAPACITY_GATE_MIN_OCCUPANCY=7
ATR_CAPACITY_GATE_MIN_ATR_15M_PCT=0.02461
```

`atr_15m_pct` is derived causally from the frozen confirmation-time `atr_15m / retest_close`. The gate is trader-side because occupancy is portfolio state. Scanner Daily-Core + Persistence V2 admission is unchanged.

# MEXC Exhaustion Scanner + Multi-Slot Futures Trader v1.3.51


## v1.3.51 — first-entry Daily Bull Persistence V1 shadow

- **Live/default strategy is unchanged:** `tp5_sl75_daily_core_skip_v1`, STANDARD+HIGH_RISK, 6×5% / 30%, TP5 + SL75, one position per symbol.
- Adds a **research-only** challenger for the first-entry blind spot exposed by the 3 Sep USELESS/PONS review. It applies only after a signal already passes the current Daily-Confirmed Core hard filter.
- Frozen V1 hypothesis: Daily Bull V1, Continuation Core V1 **false**, daily entry price `>=4.5 ATR` above EMA20D, one-day EMA20D slope `>=7.5%`, and run→breakdown `<=6h`.
- The rule is deliberately **not live** because it was designed after observing USELESS/PONS. Retrospective results are hypothesis-generating only. A clean true-forward cohort starts **03 Sep 2026 20:53 CEST**.
- Research Intelligence adds a **First-Entry Trend Persistence • V1** card comparing the current Daily-Core book against an additional persistence hard-skip shadow, including exact full-path MTM/DD when DB path rows are available.
- `research-signal-dataset.csv` exports the V1 state and true-forward flag; `volatility-research.csv` exports the bucket and portfolio comparisons.
- No database migration, env-var change, scanner threshold change, subscriber change, position resize, or live entry change.

## v1.3.50 — Daily-Core trader decision constraint hotfix

- Adds migration `017_daily_core_trader_decisions.sql` so PostgreSQL accepts `ignored_daily_core_filter` and `ignored_missing_daily_core_data`.
- Repairs v1.3.49 `decision=error` rows caused specifically by the stale `trader_signal_decisions_decision_check`, restoring the intended Daily-Core skip decision (including already-processed signals such as AKE if present).
- Corrects the trader error-alert wording: signal-processing exceptions advance the trader cursor and therefore are not necessarily retried; recurring scanner/trader work still continues normally.
- No strategy, sizing, signal threshold, TP5/SL75, slot, exposure, subscriber admission, or research-shadow logic changed. Live/default remains `tp5_sl75_daily_core_skip_v1` at 6×5% / 30%.

## v1.3.49 — hard-filter exposure challengers + true-forward 5×10 shadow

- Live/default remains `tp5_sl75_daily_core_skip_v1` at 6×5% / 30% max exposure.
- Research-only full-path challengers: 5×7.5% / 37.5% and 5×10% / 50%.
- Frozen true-forward sizing shadow starts 02 Sep 2026 09:13 CEST and compares live 6×5% against 5×10% on identical future hard-filtered signals.
- No signal thresholds, hard-filter admission logic, TP5, or SL75 settings changed.


Execution + research release. **TP5 + SL75 + Daily-Confirmed Core hard skip is now the default live/paper trader and subscriber admission strategy.** Raw confirmed signals remain stored for research.

## v1.3.48 — promote Daily-Confirmed Core hard filter live

- New default trader strategy: `tp5_sl75_daily_core_skip_v1`. Admitted signals use fixed **5% current-equity** sizing, TP **+5%**, catastrophic SL **-75%**, 6 generic slots, 30% max aggregate exposure, STANDARD+HIGH_RISK, 1x cross, and one position per symbol.
- Frozen hard filter: **skip when Continuation Core V1 AND Daily Bull V1 are both true**. Continuation Core V1 remains `run_score >= 5`, 4h EMA20 extension `>=3 ATR`, and (`previous_momentum_1h > 0` OR cross-section percentile `>=0.99`). Daily Bull V1 remains completed Day1 close > EMA20D, EMA20D slope >0, and 3D momentum >0.
- The scanner now reconstructs the completed-Day1 state **on demand at confirmation time**. It does not poll Day1 candles for the whole universe.
- Raw confirmed signals are always inserted into the signal/research pipeline first. Subscriber Discord then hard-filters flagged signals; the trader independently rechecks the **same shared classifier** before order admission.
- Missing Core/Day1 inputs are **fail-closed**: the raw signal stays in research, but subscriber publication and trader entry are both suppressed.
- Subscriber performance board now compares the **previous PCR strategy** with the **current Daily-Core hard-filter strategy**, including the real capacity benefit from skipped entries.
- PCR remains an explicit rollback strategy via `TRADER_EXECUTION_STRATEGY=tp5_sl75_pcr_v1`; subscriber rollback is `SUBSCRIBER_SIGNAL_STRATEGY=all_confirmed`.
- No database migration is required. Existing live positions are not resized or reclassified; the hard filter applies to new confirmed signals. A new default paper run ID (`tp5_sl75_daily_core_skip_v1`) intentionally starts a clean paper experiment when promoted.

### Required deployment values

Scanner service:
```text
SUBSCRIBER_SIGNAL_STRATEGY=tp5_sl75_daily_core_skip_v1
```

Trader service:
```text
TRADER_EXECUTION_STRATEGY=tp5_sl75_daily_core_skip_v1
TRADER_PAPER_RUN_ID=tp5_sl75_daily_core_skip_v1
TRADER_ALLOWED_RISK_TIERS=STANDARD,HIGH_RISK
TRADER_MAX_OPEN_POSITIONS=6
TRADER_SLOT_ALLOCATION_PCT=5
TRADER_MAX_TOTAL_EXPOSURE_PCT=30
TRADER_TP5_TARGET_PCT=5
TRADER_CATASTROPHIC_STOP_PCT=75
TRADER_MARGIN_MODE=cross
TRADER_LEVERAGE=1
```



## v1.3.45 — 7D cutoff × Daily-Confirmed Core replay

- Adds a research-only replay of the **pure 168h / 7-day hold** on the same Daily-Confirmed Core computable cohort. The 7D exit policy remains unchanged: no TP and no SL.
- Compares four chronological 6-slot / 30% books: **7D fixed 5%**, **7D + Core V1 sizing**, **7D + Daily-Confirmed Core sizing**, and **7D + skip Daily-Confirmed Core**.
- The sizing overlays use the already-frozen rules only: flagged positions are 2.5%, otherwise 5%. The skip variant excludes Daily-Confirmed Core flagged entries entirely. No threshold is retuned.
- Adds a separate **7D Hold • Daily-Confirmed Core Replay** Discord research card and exports the four portfolio rows to `volatility-research.csv`.
- Research-only: live/default execution remains `tp5_sl75_pcr_v1`; no existing position is resized and no DB migration/backfill is required when daily features are already populated.

## v1.3.44 — Daily-Confirmed Core V1 replay + true-forward monitor

- Freezes **Daily-Confirmed Core V1** at **01 Sep 2026 23:25 CEST**, immediately after the first 190-computable Core×1D matrix review.
- The rule is the exact intersection of already-frozen rules: `Continuation Core V1 == true AND Daily Bull V1 == true`. No new threshold is fitted.
- Research sizing is **2.5%** only for that intersection and **5%** for all other computable signals, with TP5 + SL75, 6 slots and 30% aggregate exposure unchanged.
- The 1D Regime card now replays **Fixed 5% vs current PCR vs Core V1 vs Daily-Confirmed Core V1** on one identical daily-computable cohort.
- Adds a clean Daily-Confirmed Core true-forward cohort beginning strictly after the 23:25 CEST freeze.
- `research-signal-dataset.csv` and `volatility-research.csv` export the new state, sizing, buckets and portfolio replays.
- Research-only: no live strategy option is added and the default trader stays `tp5_sl75_pcr_v1`. If v1.3.43 daily backfill already completed, no backfill rerun is required.

## v1.3.43 — completed-1D regime backfill + Core×daily matrix

- Adds a **research-only completed-1D regime layer**. Daily Bull V1 is intentionally structural and unfitted: last completed Day1 close above EMA20D, EMA20D slope >0, and 3-day momentum >0.
- Stores additional causal daily context (`daily_momentum_7d`, daily EMA20/ATR extension, higher-high / higher-low flags) for analysis, but these extra fields do **not** alter the Daily Bull V1 label.
- Research history maintenance now syncs `Day1` candles in addition to 15m/4h history and causally reconstructs daily features at each original signal timestamp. Incomplete current-day candles are excluded.
- Adds `python -m app.research_daily_regime_backfill_now` to fetch the required 45-day Day1 history, drain bounded DB backfill batches, retry incomplete rows once, and print `computable / missing / bullish` counts.
- Adds a separate **1D Regime • Core Context** research card with the four `Core no/yes × 1D not-bullish/bullish` cells, showing TP5, SL75, -50/-75 tail rates and average marked return.
- `research-signal-dataset.csv` exports the daily regime inputs/state plus the Core×1D cell label for every signal.
- No live sizing/entry change, no Core promotion, and no DB migration. Default trader remains `tp5_sl75_pcr_v1`.

## v1.3.42 — Continuation Core V1 true-forward monitor

- Keeps the frozen Continuation Core V1 rule unchanged and **research-only**; live/default execution remains `tp5_sl75_pcr_v1`.
- Starts a conservative true-forward Core V1 cohort at **01 Sep 2026 21:57 CEST**, after the 194-signal discovery/replay, so none of the data used to design the rule can leak into its prospective evidence.
- Forward Evidence now shows Core flagged TP5/SL75/open counts, common-cohort **Fixed vs PCR vs Core** MTM/DD/R-DD, and PCR/Core overlap.
- Tracks the decisive **Core-only** cohort separately: signals Core de-risks while PCR leaves at 5%, including its TP5/SL75/open mix and average marked return.
- `research-signal-dataset.csv` adds `continuation_core_v1_true_forward` and `continuation_core_v1_vs_pcr`; `volatility-research.csv` adds true-forward Core buckets and portfolio rows.
- No DB migration and no HTF backfill rerun are required when v1.3.40 already completed with `missing=0`.

## v1.3.41 — Continuation Core V1 research challenger

- Freezes a new **research-only** tail-risk challenger from the 1 Sep 2026 194-signal review: `run_score >= 5`, 4h EMA20 extension `>=3 ATR`, and (`previous_momentum_1h > 0` OR cross-sectional percentile `>=99%`).
- Flagged signals are replayed at **2.5%** versus **5%** otherwise, with the same TP5 +5%, SL75 -75%, 6-slot / 30% portfolio mechanics.
- Research Intelligence adds a separate Continuation Core V1 section comparing **Fixed / current PCR / HTF V1 / Continuation Core V1** on one common computable cohort, plus PCR/Core overlap counts.
- `research-signal-dataset.csv` exports the frozen Core V1 classification and sizing state; `volatility-research.csv` exports the Core V1 portfolio replay.
- The rule is intentionally **not selectable for live execution yet**. Default/live trader remains `tp5_sl75_pcr_v1`; no existing position is resized or reclassified.

## v1.3.40 — bounded HTF DB backfill + one-shot recovery command

- Fixes the deployed v1.3.39 HTF reconstruction timeout observed in `backfill_research_htf_features()`. Historical candle predicates now keep indexed `candles.open_time` bare (`open_time <= confirmed_at - interval ...`) so PostgreSQL can use the existing `(symbol, interval, open_time)` index efficiently.
- Reduces HTF reconstruction to bounded 64-episode batches and applies the research DB statement timeout per batch instead of allowing one large optional research query to monopolize the shared database.
- HTF retry work now ignores rows whose only missing input is `cross_section_percentile`, because that field is intentionally not fabricated. Recoverable missing rows use a stable retry frontier so one repeatedly incomplete episode cannot starve the rest of the backlog.
- Adds `python -m app.research_htf_backfill_now`: drains never-attempted HTF rows in bounded batches, makes one bounded pass over recoverable legacy misses, refreshes HTF metadata, and prints final `computable / missing / flagged` counts.
- Worker research and HTF-history loops pass `RESEARCH_DB_TIMEOUT_SECONDS` into HTF reconstruction. Live/default trader behavior remains `tp5_sl75_pcr_v1`; no strategy switch or position resize occurs on deploy.


## v1.3.39 — true HTF feature reconstruction + common-cohort replay

- Adds an automatic **historical HTF feature reconstruction pass** before HTF classification. Missing fields are recovered only from information available at or before each original signal timestamp.
- Recovery order is auditable and persisted per feature: frozen prior `run_signals` snapshot → recent stored MEXC ticker snapshot for exact 24h return → pre-signal 15m/4h candle reconstruction. A candle-derived 24h return is explicitly tagged `min15_24h_proxy`.
- **Historical `cross_section_percentile` is never fabricated.** It is recovered only from a frozen prior signal snapshot; if unavailable, HTF V1 remains non-computable for that signal.
- Backfill metadata now records feature provenance, backfill timestamp, approximation fields, tri-state computability, flag, missing fields and the resulting HTF position fraction. After deeper history is fetched, non-computable rows are retried automatically.
- Research Intelligence now replays **Fixed 5% / current PCR / HTF V1 / PCR+HTF** on the exact same HTF-computable signal cohort, so missing legacy rows cannot bias the challenger comparison.
- The report also shows PCR/HTF overlap. Under the frozen V1 rules, HTF is a strict subset of PCR (same 24h + 4h core thresholds plus two extra requirements), so `PCR + HTF` is intentionally expected to equal PCR; the useful question is whether the narrower HTF sizing rule preserves more winning exposure while retaining tail protection.
- Default/live execution remains **`tp5_sl75_pcr_v1`**. No existing position is resized and no strategy switch occurs on deploy.

## v1.3.38 — HTF unresolved-bull-regime de-risk challenger

Adds a separate `tp5_sl75_htf_v1` strategy using the frozen 1 Sep 2026 rule: 24h return >=30%, cross-sectional percentile >=98%, 4h EMA20 extension >=3 ATR, and previous 1h momentum >0. Flagged entries use 2.5% of equity; otherwise 5%, with the same TP5 + SL75 exits, 6 slots and 30% cap. The existing `tp5_sl75_pcr_v1` remains the default. HTF classification is tri-state: missing required inputs are explicitly marked non-computable and an HTF-selected trader fails closed rather than treating missing data as unflagged. Research sync backfills HTF metadata into frozen snapshots and pre-signal history collection now covers 15m plus 4h data for future reconstruction/multi-timeframe exhaustion research.

## v1.3.37 — subscriber performance board: fixed TP5+SL75 vs PCR

- **Subscriber Discord performance board only:** replaces the old TP5-indefinite / TP5+SL75 / 7D presentation with the two strategies users can actually compare now: previous live **TP5+SL75 fixed 5%** versus current **TP5+SL75 PCR de-risk**.
- Preserves the existing subscriber statistics: all-signal net sum / average / positive rate, TP5/SL75/open counts, holding-time stats, chronological account MTM return, 30D-equivalent run-rate, max DD, return/DD, capture, closed/open counts, capacity/symbol misses, and average/peak exposure.
- Adds a dedicated PCR account replay to `performance.py`, using the same frozen signal-time PCR rule as the live trader: 24h return >=30% and 4h EMA20 extension >=3 ATR => 2.5%; otherwise 5%. Entries, TP5, SL75, slots and symbol rules are identical to the fixed competitor.
- `performance_rows()` now reads each signal's frozen feature snapshot so the subscriber PCR replay uses signal-time features without look-ahead.
- **Research Intelligence is unchanged from v1.3.35.** A/B/C, STANDARD scaling, ATR, parabolic research, forward evidence and research CSV exports remain intact.
- Trader execution is unchanged from v1.3.35; this release does not reset or resize the existing PCR paper/live run.

## v1.3.35 — PCR de-risk promoted to live/default sizing

- **New default execution strategy:** `tp5_sl75_pcr_v1`. Entry signal logic, STANDARD+HIGH_RISK eligibility, TP5, SL75, one-position-per-symbol, 6-slot limit, 30% exposure cap and 1x cross are unchanged.
- **Only sizing changes for new entries:** frozen PCR flag = signal-time 24h return >=30% **and** distance above 4h EMA20 >=3 ATR. Flagged entries use **2.5%** of current equity; all other entries use **5%**.
- The PCR thresholds are code-frozen and shared by live execution and research analytics so the implementation cannot silently drift from the replay rule.
- Existing **live** positions are not resized or reclassified on deployment; their persisted exit strategy remains unchanged. The default paper run ID changes to `tp5_sl75_pcr_v1`, so a paper deployment intentionally archives the prior paper run and starts a clean $2,000 PCR-forward run unless you override the run ID.
- `tp5_sl75_v1` remains supported as an explicit rollback strategy with fixed 5% sizing.
- Live catastrophic-stop behavior is unchanged: every new PCR/default position still persists `tp5_sl75_full` and must establish the exchange-side SL75 protection.

## v1.3.34 — STANDARD 10×5% + SL75 promoted as challenger

- **Research only; live trader unchanged:** `TP5_SL75_V1` remains STANDARD+HIGH_RISK at 6×5% / 30%.
- Promotes the already-computed **STANDARD-only TP5 + SL75, 10 slots × 5% / 50% cap** replay to the main research board as strategy **D**.
- Keeps the pure STANDARD TP5 10×5% replay beside it as a no-stop twin, so the report directly measures the historical cost/benefit of catastrophic protection at the proposed capacity.
- Exports `standard_tp5_sl75_10x5` in `strategy-validation.csv`, including exact synchronized MTM drawdown, return/DD, capture, exposure and tail/recovery evidence.
- Forward/frozen evidence now reports the same STANDARD 10×5% + SL75 challenger, rather than using only the 10×10% safety stress case.
- 7.5% and 10% STANDARD slot sizes remain **stress-size research**, not deployment recommendations.

## v1.3.33 — parabolic continuation-risk research


Research-only continuation-risk challenger:
- frozen flag: `return_24h >= 30%` AND `distance_above_ema20_atr_4h >= 3.0`
- flagged positions shadow-size at 2.5% of equity; all other positions remain 5%
- same 6 generic slots, 30% nominal exposure cap, TP5 + SL75, one-open-position-per-symbol
- full-sample and post-freeze MTM, max synchronized drawdown, return/DD and flagged cohort outcomes are reported
- thresholds are intentionally not swept after observing PONS/HNT/CATE; this remains research-only
- live/default execution is unchanged

- **Research only; live trader unchanged:** default execution remains `TP5_SL75_V1`, STANDARD+HIGH_RISK, 6×5% / 30%, TP5 + catastrophic SL75.
- Exposes the already-frozen confirmation-time `atr_15m` feature and derives **`atr_15m_pct = atr_15m / entry_price`** so volatility is comparable across coins without look-ahead.
- Freezes ATR% quartile boundaries and the sizing anchor from the **pre-21-Aug discovery cohort only**; post-freeze analysis reuses those frozen thresholds.
- Adds volatility quartiles for all signals plus STANDARD/HIGH_RISK tier splits with TP5 hit-to-date, TP5 speed, average SL75-policy marked return and adverse -20/-50/-75/-100 rates.
- Adds ATR% to generic feature-lift research and interaction research versus 4h EMA extension and run score.
- Adds a fair fixed-vs-volatility-normalized portfolio replay under the **same 6-slot / 30% total exposure cap**. Normalized sizing is `5% × frozen_median_ATR% / signal_ATR%`, clamped to **2.5%–7.5% per trade**; missing ATR falls back to 5%.
- Adds `volatility-research.csv` and Discord full-sample/post-freeze volatility evidence.
- No database migration is required because ATR14 was already stored inside confirmed-signal feature snapshots.


## v1.3.31 — TP5+SL75 schema constraint fix

- Adds migration `016_tp5_sl75_exit_strategy.sql`.
- Fixes PostgreSQL `trader_positions_exit_strategy_check` so the persisted exit strategy `tp5_sl75_full` is accepted.
- Preserves every previously allowed exit strategy; no position data rewrite is required.
- Trader/research strategy logic is otherwise unchanged from v1.3.30.

## v1.3.30 — STANDARD-only scaling research challenger

- **Research only; live trader unchanged:** the default remains `TP5_SL75_V1`, 6×5% / 30% across STANDARD + HIGH_RISK.
- Adds a **STANDARD-only TP5 scaling curve** using the same chronological replay engine, fees, one-symbol rule and stored 15m MTM paths: **10×5% / 50%**, **10×7.5% / 75%**, and **10×10% / 100%**.
- Adds a **10×10% + SL75 safety twin** so the research report can quantify whether catastrophic protection changes return or drawdown once HIGH_RISK is excluded.
- Discord research now reports each scale point's MTM return, 30-day-equivalent run-rate, exact synchronized max drawdown, return/DD and capture rate, plus post-freeze versions.
- `strategy-validation.csv` exports each scale point as a separate strategy row for LLM/human comparison.
- No scanner entry rule, trader execution rule, database schema or Render live sizing changed.

## v1.3.30 — research analytics timeout fix

- **Fixed `research_analytics_rows()` timeout:** removed the legacy PostgreSQL query that repeatedly used ordered `array_agg()` over the growing 15m research-path table.
- **Fixed the same scaling problem in `performance_rows()`:** `signal_ledger_now`, `report_now`, and scheduled performance reporting now use lightweight path reads plus Python aggregation instead of the old ordered-array SQL.
- **Single lightweight analytics path read:** the on-demand analytics process now reads the public signal rows plus the required path columns once, aggregates per-signal path statistics in Python, and reuses that same in-memory path set for portfolio MTM replay. Performance/ledger workflows use the same lightweight aggregation approach.
- **No research semantics changed:** TP5-indefinite, TP5+SL75, pure 7D-hold outcomes, adverse-threshold timing, 7d/14d MFE/MAE and horizon marks preserve the previous definitions.
- **No trader execution change and no database migration required.**

## v1.3.28 — focused performance playbook + research intelligence

- **Correct 7D benchmark:** strategy C is now a pure 7-day hold. It has **no profit target and no stop**; every entered short is closed exactly 168 hours after confirmation at the observed 7D return.
- **Subscriber performance board:** separates **all-signal economics** (Σ/average/positive marked trade returns if every qualifying signal were taken) from the **replicable 6×5% / 30% account replay** (observed return, 30D-equivalent run-rate, MTM drawdown, capture rate, exposure, open positions).
- **Replication playbook:** explicitly documents STANDARD+HIGH_RISK entries, 1× cross, 5% current-equity sizing, six slots, 30% cap, one position/symbol, fees, and exact exit rules. TP5+SL75 remains the live default.
- **Research intelligence:** Discord now interprets the collected evidence: tail-breach/recovery rates, SL75 insurance effect versus indefinite TP5, pure-7D benchmark economics, TP5 holding-time/tail aging, capacity capture, exploratory feature clues, and post-freeze validation.
- **LLM-ready strategy export:** `strategy-validation.csv` now includes all-signal marked economics, capture rate, 30D-equivalent portfolio return, account return/DD and tail/recovery counts. `research-signal-dataset.csv` uses explicit `hold_7d_*` outcome fields.
- Trader execution logic is unchanged from v1.3.27 apart from the heartbeat/version string. No database migration is required.

## v1.3.26 — no-timeout TP5 validation + MTM query fix

- **Portfolio MTM timeout fixed:** the on-demand MTM query no longer joins/sorts the full research path before returning it. It reads the three required path columns directly and lets the replay build/sort its own event timeline, eliminating the expensive query shape that Render canceled at the 10s statement timeout.
- **MTM no longer stops at day 7:** portfolio path retrieval has no 168h cutoff. TP5/no-stop and TP5+SL75 validation can use every stored post-signal mark.
- **Research path storage now follows unresolved TP5 positions beyond the normal 14d research horizon:** `RESEARCH_PATH_HORIZON_HOURS=336` remains the minimum fixed research window; if +5% has still not occurred, 15m path collection continues until TP5 is observed. This keeps no-stop validation aligned with the trader instead of silently freezing an old mark.
- **Strategy Validation corrected:** TP5 no-stop is evaluated across every observed signal and counts a +5% target whenever it is eventually reached, including after day 7. Unresolved positions remain open/waiting indefinitely; they are never classified as failed merely because a 7-day path matured.
- **7d research remains separate:** seven-day maturity is still used for fixed-horizon feature/return studies and paired 7d cohorts, but it no longer defines TP5 strategy success or position lifetime.
- **TP5+SL75 added explicitly to the validation board:** the default strategy now shows TP5-first, SL75-first, waiting/open, resolved TP rate, portfolio return, realized return, drawdown, entries, closes, and capacity misses beside the TP5 no-stop baseline.
- **30-day validation compares both TP5 variants** when a true 30-day empty-book window is available.
- **Prospective monitor corrected:** reports hit, waiting/open, open >7d, observed hit-to-date rate, and oldest waiting age; the old `failed after complete 7d` classification is removed.
- No execution-rule or schema change from v1.3.24; TP5+SL75 remains the default trader.

## v1.3.24 — TP5 + catastrophic -75% stop

- **New default trader strategy:** `tp5_sl75_v1` keeps the proven TP5 structure: STANDARD + HIGH_RISK, 6 generic slots, 5% current-equity notional per slot, 30% aggregate exposure, 1x cross, one open position per symbol, full close at +5%.
- **Catastrophic stop:** new positions close fully at **-75% short return** if that threshold is reached before TP5. This is deliberately a far-tail risk cap, not a tight trading stop.
- **Live safety:** live `tp5_sl75_v1` positions place an exchange-side MEXC position stop immediately after the short is confirmed. If protection cannot be placed, the bot attempts to close the newly opened position and raises an error rather than intentionally leaving it unprotected.
- **Legacy compatibility:** persisted `tp5_full` positions from `tp5_v1` retain their old no-stop behavior. Newly opened default positions persist `tp5_sl75_full`, preventing a silent mid-trade mutation on deployment.
- **Research comparator:** `tp5_sl75_challenger_6x5pct` is replayed beside TP5 without a stop using `target_5_at` versus `adverse_75_at`; same-15m-candle races are conservatively counted stop-first. The comparison is exported in research strategy CSVs and surfaced on the research Discord board.
- **New config:** `TRADER_CATASTROPHIC_STOP_PCT=75` (default).
- **New paper run:** Render defaults to `TRADER_PAPER_RUN_ID=tp5_sl75_v1`; deploying over an older paper run intentionally archives the previous run and starts the new strategy at configured paper starting equity.
- No schema migration is required; migration `015_tp5_trader_runs.sql` remains latest.


## v1.3.22 — research-only persistent-run continuation-risk flag

- **New research-only flag; no execution change:** `hours_run_to_breakdown >= 36h` is tracked as `persistent_run_long_flag`. A stricter candidate, `>=36h` plus `distance_above_ema20_atr_4h <= 3`, is tracked as `persistent_run_strict_flag`. Neither affects signal publication, TP5 trader entry, sizing, targets, or subscriber strategy eligibility.
- **True prospective freeze:** thresholds are frozen at **2026-08-26 13:22 UTC / 15:22 CEST**. The retrospective calibration is evaluated only with information knowable at that freeze; future signals are placed in a separate prospective cohort.
- **Censor-safe -100% evaluation:** the research endpoint is a -100% adverse move within the first **120h**. An early breach resolves immediately; a non-breach is counted only after a full 120h path. This prevents young signals from being mislabeled safe.
- **Frozen calibration reproduced:** long-run signals show **5/17 (29.41%)** -100% breaches versus **4/57 (7.02%)** for shorter runs; the strict flag shows **4/10 (40.00%)** versus **5/64 (7.81%)** for other evaluable signals. These are small-sample research statistics, not a validated filter.
- **Research Discord tracking:** Strategy Validation now shows the frozen calibration and a forward tracker for long/strict flagged signals and their censor-safe 120h -100% breach rates.
- **Dataset audit fields:** the daily research CSV includes `persistent_run_long_flag`, `persistent_run_strict_flag`, and `persistent_run_risk_cohort`.
- **Frozen TP5_V1 trader unchanged.** No schema migration; `015_tp5_trader_runs.sql` remains latest.

## v1.3.21 — two recommended strategies + account drawdown

- **Subscriber board simplified to two actionable choices:** TP5 Frequent and STANDARD 7D Swing. TP20 No Timeout is removed from the public recommendation/account-comparison card because its current observed account return is lower while capacity congestion and tail exposure are materially worse.
- **TP20 is not deleted:** it remains fully calculated in the Strategy Ledger and research layer for both STANDARD and HIGH_RISK signals, including target timing, MTM, and breach-before-target evidence. It can be promoted again if forward evidence improves.
- **Account risk added:** each recommended strategy now reports **max MTM drawdown** and **observed return / max drawdown** beside observed account return and 30-day equivalent run-rate.
- **15-minute portfolio marks:** `performance_rows()` now returns the stored 15m signal path timestamps/returns so account drawdown is reconstructed from the exact trades admitted by chronological slot/capacity replay, plus exact entry/exit/report events.
- **Clearer subscriber wording:** the headline is now `Strategy Account Performance • Suggested Sizing`, and the dollar illustration reads `30D eq. ≈ $X per $10k` so it cannot be mistaken for already-realized P&L.
- **7D sizing remains explicitly risk-based; TP5 sizing remains the frozen portfolio-tested 5% × 6 / 30% cap.**
- **Frozen TP5_V1 trader execution unchanged.** No schema migration; `015_tp5_trader_runs.sql` remains latest.

## v1.3.20 — ledger is fully observational across tiers

- **TP20 ledger is observational, not recommendation-gated:** STANDARD and HIGH_RISK rows both show the +20% no-timeout outcome/path, including target time, live MTM if unresolved, and -50/-100/-200/-300 breaches before target/current mark.
- **Subscriber strategy remains unchanged:** the public/account-level TP20 recommendation and replay remain HIGH_RISK-only; this change only prevents STANDARD TP20 evidence from being hidden as `N/A` in the ledger.
- **7D evidence is observational too:** HIGH_RISK rows also show the 7-day hold outcome/path in the ledger; the public 7D Swing recommendation and account replay remain STANDARD-only.
- **Frozen TP5_V1 trader execution unchanged.**
- **No schema migration:** migration `015_tp5_trader_runs.sql` remains latest.

## v1.3.19 — unambiguous ledger status vs breach warnings

- Primary cell color now represents strategy status only: green = completed target/win, amber/blue = open, red = closed loss.
- Pre-target/pre-exit breaches are rendered as a separate red warning line and never repaint a winning TP5/TP20 outcome red.


- **Ledger now mirrors the three selected subscriber strategies:** TP5 Frequent, HIGH_RISK TP20 No Timeout, and STANDARD-only 7D Swing.
- **Per-row strategy outcome:** the PNG ledger shows target hit/open/7D close, current or realized strategy return, and elapsed holding time. Ineligible combinations are explicitly `N/A` rather than silently omitted.
- **Breach-before-exit is strategy-specific:** each strategy cell reports the deepest -50/-100/-200/-300 adverse threshold reached **before that strategy's target/exit**. Open/tracking trades show the deepest breach observed so far. Later breaches after an earlier TP5/TP20 exit do not count against that strategy.
- **CSV is strategy-first:** explicit TP5, TP20, and 7D status/return fields plus threshold-by-threshold breach-before-target/exit flags are placed before the retained raw audit fields.
- **15m -50% breach data is now exposed by `performance_rows()`:** the ledger and Strategy Comparison use the same research path source for -50/-100/-200/-300 timestamps.
- **Worst-adverse reporting fixed:** `path_mae_before_target_20` and `path_mae_7d` are now returned, allowing TP20 and 7D normalized comparison cards to populate worst adverse excursion instead of `n/a` when path data exists.
- **Frozen trader unchanged:** TP5_V1 execution remains byte-identical to v1.3.17.
- **No schema migration:** migration `015_tp5_trader_runs.sql` remains latest.


## v1.3.17 — account-level 30-day equivalent run-rate

- **Account return is now the headline comparison:** each subscriber strategy is replayed chronologically with its recommended per-trade sizing, slot cap, one-symbol collision rule, and 0.08% shadow fee per fill.
- **Same observed calendar:** TP5, TP20 No Timeout, and STANDARD 7D all replay from the same first public signal through the report timestamp.
- **Actual exit semantics:** TP5 exits at +5%; TP20 exits only at +20% and otherwise remains open; STANDARD 7D exits exactly at 168h. Open positions are marked to current market at report time.
- **30-Day Equivalent Run-Rate:** linearly scales observed account return to 30 days and shows an equivalent P&L per $10k. It is explicitly labeled an extrapolated run-rate, not an observed 30-day result or forecast.
- **Capacity is included:** report shows entered trades, currently open positions, capacity misses, and average/peak account exposure under the suggested configuration.
- **Raw signal Σ is demoted:** the normalized 168h TP5/TP20/7D table remains as supporting path evidence and is labeled `Σ signal`, preventing raw opportunity sums from being confused with account return.
- **Fresh TP20 MTM beyond day 7:** on-demand and scheduled reports refresh current marks for old HIGH_RISK signals because TP20 has no timeout. If an open position cannot be marked, account/run-rate output is withheld instead of guessed.
- **Cost caveat:** run-rate includes the 0.08%/fill shadow fee but does not model funding or slippage.
- **No schema migration:** migration `015_tp5_trader_runs.sql` remains latest.

## v1.3.16 — normalized three-strategy subscriber comparison

- **One canonical public Strategy Comparison board:** TP5 Frequent, TP20 High Risk No Timeout, and STANDARD 7D Swing.
- **Apples-to-apples 168h valuation:** every headline strategy return is valued exactly 7 days after signal confirmation. TP5/TP20 lock +5%/+20% when hit before 168h; otherwise the still-open trade is marked at its 7-day return. This convention is comparison-only and does **not** add a timeout to TP20.
- **Comparable performance metrics:** sample n, target hits/open-at-7d, profitable marks, arithmetic Σ equal-notional signal return, average/median per signal, best/worst, and effective capital time.
- **Comparable path risk:** -50%, -100%, -200%, and -300% adverse breaches are counted only while the strategy is still exposed, plus worst observed adverse excursion before target/7-day mark.
- **Suggested subscriber exposure:** TP5 5% × 6 / 30% cap (the frozen portfolio-tested setup); TP20 2% × 5 / 10% cap; STANDARD 7D 3% × 5 / 15% cap. TP20/7D sizing is clearly labeled risk-based rather than return-optimal or validated.
- **Research Discord no longer duplicates strategy rules:** it keeps evidence health + the frozen TP5 prospective monitor and points subscribers to Strategy Comparison for TP5/TP20/7D selection.
- **EXTREME_RISK remains suppressed** before episode/signal creation and never reaches public alerts.
- **No schema migration:** migration `015_tp5_trader_runs.sql` remains latest.

## v1.3.15 — add HIGH_RISK TP20-or-4D public strategy

- **Three public strategy choices only:**
  - **TP5 Frequent:** STANDARD + HIGH_RISK, full +5% exit.
  - **TP20 High Risk:** HIGH_RISK only, full +20% exit or close at 4 days.
  - **7D Swing:** STANDARD only, fixed 7-day hold.
- **TP20 metrics are strategy metrics, not raw horizon metrics:** Discord shows paired mature sample, realized wins/losses, realized win rate, TP20 hit rate, average/median/Σ realized return, best/worst, and average holding time.
- **Strict denominator:** TP20-or-4D uses the same complete/evaluable paired 10-day HIGH_RISK cohort used to compare 1d–10d timeouts, so the 4-day result remains apples-to-apples with the research that selected it.
- **EXTREME_RISK remains suppressed** before episode/signal creation and never reaches public alerts.
- **Research Discord stays compact:** Strategy Validation now shows exactly TP5, TP20 High Risk, and STANDARD 7D, plus the separate Prospective TP5 Monitor. Exploratory alternatives remain internal.
- **No schema migration:** migration `015_tp5_trader_runs.sql` remains latest.

Frozen TP5_V1 execution remains: 6 generic STANDARD/HIGH_RISK slots, 5% current equity each, 30% aggregate exposure, 1× cross, immediate entry, one open position per symbol, full close at +5%, 0.08% paper fee per fill.

## v1.3.5 — calendar throughput research

## v1.3.4 — prospective monitoring

- Added immediate post-freeze TP5 hit/wait/fail monitoring.
- Added rolling-20 EntryGate-v1 acceptance and discovery-vs-post-freeze regime diagnostics.
- Added the post-freeze four-way paired portfolio table.

## v1.3.3 — prospective strategy lab

- Added 15-minute close-marked MTM portfolio replay for the current strategy and TP5 challenger.
- Froze **EntryGate-v1** at Entry Quality >= 4 and Continuation Risk <= 6; shadow-only.
- Added four paired complete-7d portfolio replays: current, TP5, EntryGate + current exits, and EntryGate + TP5.
- Froze the prospective OOS boundary at **2026-08-21 21:29 UTC / 23:29 CEST** and separated discovery from post-freeze evidence.

## v1.3.1 — Discord formatting hotfix

Hotfix: restores Discord formatting helpers used by signal and performance reports. Fixes `AttributeError: DiscordNotifier has no attribute _percent` in both scheduled and on-demand reports.

# v0.8.1 — wide 72h discovery hotfix

This hotfix fixes a remaining discovery gap for low-liquidity coins such as CASHCAT.

- Every active MEXC crypto perpetual now receives a lightweight Hour4 scan once per hour.
- Any contract up at least 20% over the rolling 72h window enters full 15m/4h analysis even if its current 24h return has already cooled.
- Late-discovered prior runners can remain in EXHAUSTION WATCH down to -25% current 24h return when reversal evidence is present.
- `DIAGNOSTIC_SYMBOLS` logs exact discovery data; the Render Blueprint defaults this to `CASHCAT_USDT` for debugging.
- Liquidity remains an execution-risk label, not a discovery gate.

# MEXC Post-Run Exhaustion Scanner — v0.7

Render-hosted, MEXC-only, shadow-mode scanner for post-run exhaustion shorts. It does not place orders.

## Signal state machine

1. `RUN WATCH`
2. `EXHAUSTION WATCH`
3. `BREAKDOWN WATCH`
4. `CONFIRMED SHORT` only after a later failed retest of the saved broken support level.

One confirmed short is allowed per pump episode. A confirmed episode can only re-arm after a materially higher new high.

## Daily performance tracker

Every `CONFIRMED SHORT` creates a shadow trade at the failed-retest candle close. The tracker stores:

- current mark-to-market short return
- maximum favorable excursion (MFE) during the first 24h
- maximum adverse excursion (MAE) during the first 24h
- 1h, 4h, 12h and 24h short returns
- 24h win/loss outcome

Once per day the worker posts a Discord summary. Defaults:

- report time: `18:00`
- timezone: `Europe/Zurich`
- performance refresh: every `300` seconds

The daily report contains confirmed shorts today, open signal mark-to-market, all-time 24h win rate, average 1h/4h/12h/24h returns, summed 24h signal return, average MFE/MAE, and best/worst 24h signal.

These are **signal analytics**, not account P&L. Fees, slippage, funding, leverage and position sizing are deliberately excluded until an execution model is defined.

Historical v0.5 `SHORT SETUP` alerts are excluded. Migration `005_performance_tracking.sql` backfills already-existing v0.6+ `CONFIRMED SHORT` signals when their retest close is available.

## Required migration

Keep migrations `001` through `004` and add:

`migrations/005_performance_tracking.sql`

Migrations are tracked in `schema_migrations` and run once.

## Performance environment variables

| Variable | Default |
|---|---:|
| `PERFORMANCE_POLL_SECONDS` | `300` |
| `PERFORMANCE_REPORT_HOUR` | `18` |
| `PERFORMANCE_REPORT_TIMEZONE` | `Europe/Zurich` |

## Expected Discord report

```text
📊 DAILY SHADOW PERFORMANCE — 2026-08-08
Confirmed shorts today: 2
Open tracked signals: 3
Open mark-to-market: +3.25% avg | +9.75% summed
24h matured signals: 12 all-time | 1 today
24h win rate: 66.67%
Average short return: 1h +1.10% | 4h +2.80% | 12h +4.20% | 24h +5.10%
Summed 24h signal return: +61.20%
Average MFE: +8.40% | Average MAE: -3.10%
Best 24h: XYZ_USDT +22.40%
Worst 24h: ABC_USDT -9.20%
Returns are measured from CONFIRMED SHORT retest close.
Analytics only: no fees, slippage, funding, leverage or position sizing included.
```

## Tests

```bash
pip install -e '.[dev]'
pytest
python scripts/verify_project.py
```

## Discord alert policy

Discord receives only `CONFIRMED SHORT` strategy alerts. `RUN WATCH`, `EXHAUSTION WATCH`, and `BREAKDOWN WATCH` continue to be stored and processed internally because they are required by the state machine, but none of them are posted to Discord. Performance reports are unchanged.

The notifier hard-gates strategy alerts to `confirmed_short`, so an older Render environment variable that still lists `exhaustion_watch` cannot re-enable exhaustion alerts.

## On-demand performance report (v0.8.5)

The scheduled 18:00 Europe/Zurich report remains unchanged. You can also send a
current report at any time from the Render background worker's **Shell** page:

```bash
python -m app.report_now
```

The command refreshes open shadow trades against the current MEXC ticker, builds
the same performance summary as the daily report, and sends it to Discord with
the heading `ON-DEMAND SHADOW PERFORMANCE`.

It deliberately does **not** insert a row into `performance_reports`, so running
it manually never suppresses or postpones the scheduled daily report.

## v0.8.6 — 48h / 72h performance horizons

Shadow performance is now tracked for 72 hours after every CONFIRMED SHORT.
The daily and on-demand Discord reports include independent 24h, 48h and 72h
sample sizes, win rates, average returns, summed returns and execution-risk
splits. MFE/MAE continue updating through the full 72-hour window so delayed
collapses after an initial squeeze are captured.

The new migration is `migrations/007_extended_performance_horizons.sql`.
After deployment, the on-demand command remains:

```bash
python -m app.report_now
```


## v0.9.0 — seven-day capital-buffer simulation

Performance tracking now continues for 168 hours after every CONFIRMED SHORT.
The Discord report separates STANDARD and HIGH+EXTREME signals and adds:

- 1d / 2d / 3d / 7d fixed-horizon returns.
- Percentage ever profitable within seven days.
- Percentage reaching +20% short return within seven days.
- Percentage experiencing a +100% adverse price move (research proxy for exhausting a 1x isolated position).
- Percentage experiencing a +400% adverse price move (configured conservative 5x equity-to-position cross-buffer breach).
- Whether either adverse threshold occurred before first profitability / before the +20% target.
- Average and summed returns among trades that had not breached the +400% threshold by each horizon.
- A 20%-sized account-equivalent summed return (0.20 x summed position returns). This is intentionally not a compounding or overlapping-position portfolio backtest.

These thresholds are research proxies, not MEXC liquidation prices. Actual liquidation depends on maintenance margin, fees, funding, other cross positions, and account equity.


## v0.9.1 — generic liquidation-survival analytics

- Keeps raw confirmed-short performance generic; no assumed take-profit or position-closing rule.
- Reports 1d / 2d / 3d / 7d raw returns and win rates by STANDARD vs HIGH+EXTREME.
- Adds a 1x isolated research overlay: +100% adverse move (price reaches 2x entry).
- Adds a 5x cross-buffer research overlay: +400% adverse move (price reaches 5x entry).
- For each horizon and risk group, reports survival rate, survivor win rate, average return and summed return for each overlay.
- Full 7d path also reports ever-profitable rate and whether each adverse threshold occurred before first profitability.
- Removes the old +20% target/account-equivalent presentation.
- Thresholds remain research proxies rather than exact MEXC liquidation prices.


## v0.9.3 — dedicated subscriber performance board

- Confirmed-short alerts continue to use `DISCORD_WEBHOOK_URL`.
- Performance reports can now use a separate Discord server/channel via `DISCORD_PERFORMANCE_WEBHOOK_URL`.
- If the dedicated stats webhook is not configured, performance reports fall back to `DISCORD_WEBHOOK_URL` for backward compatibility.
- Performance output is now a four-card Discord embed board: overview, STANDARD, HIGH+EXTREME, and survival-methodology.
- Raw signal analytics remain generic: no take-profit, stop-loss, leverage, or position-sizing rule is assumed.
- 1d/2d/3d/7d isolated (+100% adverse) and 5× cross-buffer (+400% adverse) research overlays remain visible in the risk cards.
- No database migration is required; keep migrations 001–008.

## v0.9.2 — short-only Discord alerts

- Discord strategy notifications are hard-gated to `CONFIRMED SHORT` only.
- `EXHAUSTION WATCH`, `RUN WATCH`, and `BREAKDOWN WATCH` remain internal strategy states.
- Existing Render environments that still contain `exhaustion_watch` in `DISCORD_SIGNAL_LEVELS` cannot cause exhaustion alerts to be posted.
- Performance reporting is unchanged.

## v1.0.0 — original trader (historical)

v1.0 introduced the second Render worker and a one-position STANDARD-only paper trader. Its `TRADER_CAPITAL_STRATEGY` and fixed-maturity execution model are retained in release history only and are superseded by v1.2.0 below.

## v1.1.0 — +20% target analytics and configurable position maturity

Performance embeds keep the same four-card layout. The STANDARD and HIGH+EXTREME horizon rows now show, for both the 1x-isolated (+100% adverse) and 5x-cross-buffer (+400% adverse) research proxies:

- percentage of matured signals that reached +20% short return before the proxy breach and before the horizon;
- average elapsed time from CONFIRMED SHORT to the first +20% observation among those hits.

The trader now exposes three independent strategy dimensions:

```text
TRADING_MODE=paper|live
TRADER_CAPITAL_STRATEGY=isolated_full|cross_20
TRADER_POSITION_MATURITY=profit_20|1d|2d|3d|7d
```

`profit_20` exits at the first observed `TRADER_PROFIT_TARGET_PCT` (20% by default). Fixed-day maturity modes hold until the configured horizon and then close at market unless the selected paper liquidation proxy is breached first. Exactly one position may be open at a time; new signals while busy remain ignored.


## v1.1.1 — horizon-independent +20% target race

The subscriber performance board now keeps fixed-horizon analytics (1d/2d/3d/7d) separate from the trader-style +20% target race. For STANDARD and HIGH+EXTREME signals it reports +20% win rate before the -100% isolated proxy and before the -400% cross-buffer proxy, pending/resolved counts, and average time to +20%. Pending races continue to be tracked beyond the 7-day fixed-return window until +20% wins or the +400% cross proxy is breached.

## v1.1.2 trader JSON hotfix

- Safely decodes `run_signals.features` whether asyncpg returns JSONB as a mapping or JSON text.
- Applies the same defensive decoding to trader position `metadata`.
- No schema migration is required.

## v1.1.3 trader market-data hotfix

The one-position trader now monitors the active MEXC futures symbol via the official public futures WebSocket ticker (`wss://contract.mexc.com/edge`, `sub.ticker`) instead of polling the REST ticker every few seconds. REST ticker access remains a retrying fallback only. This avoids intermittent MEXC code 510 rate-limit errors while preserving paper/live execution semantics.


## v1.1.4
- Explicit `websockets>=15,<17` runtime dependency for the trader ticker stream.
- WebSocket import is now lazy, so a missing package cannot crash the trader at module import; REST fallback remains available.


## v1.1.5 — strategy viability matrix

- Subscriber stats now compare STANDARD vs HIGH+EXTREME across +20% target, 1D, 2D, 3D and 7D profitability strategies.
- Each strategy is evaluated against -100%, -200%, -300% and -400% adverse-move research thresholds.
- A horizon strategy wins only when its exact-horizon return is positive and the selected threshold was not breached beforehand.
- +20% target remains horizon-independent and reports pending outcomes separately.
- Average and summed profit are highlighted only for strategy/threshold cells with 100% observed win rate.
- Migration 011 adds/backfills -200% and -300% breach timestamps and refreshes 7-day first-breach data for older signals.

## v1.1.6 — Discord strategy-board payload hotfix

Discord caps the combined textual content across all embeds in one message at 6,000 characters. The expanded strategy matrices can exceed that budget when overview, STANDARD, HIGH+EXTREME and methodology are sent together. v1.1.6 sends the same four visual cards as four consecutive webhook messages and validates Discord embed limits before sending. No database migration is required.

## v1.1.7 — fixed-horizon loss breakdown
Fixed-horizon strategy-matrix cells now split failures into mutually exclusive reasons: threshold breach before maturity versus not profitable at the exact maturity. Win-rate semantics and trader behavior are unchanged.


## v1.1.8 — HIGH vs EXTREME stats split

Performance Discord now renders STANDARD, HIGH RISK, and EXTREME RISK as separate strategy-matrix cards. The previous combined HIGH+EXTREME calculations remain available internally for compatibility, but subscriber-facing stats no longer average the two risk tiers together. No database migration is required.

## v1.1.9 — per-signal outcome ledger

A second, on-demand subscriber report now complements the aggregate strategy board:

```bash
python -m app.signal_ledger_now
```

The report posts to `DISCORD_PERFORMANCE_WEBHOOK_URL` (falling back to the signal webhook for backward compatibility) and includes:

- every confirmed-short signal, grouped into STANDARD / HIGH RISK / EXTREME RISK;
- confirmed-short time and signal price;
- whether +20% was reached and elapsed time to target;
- reconstructed 1D / 2D / 3D / 7D prices plus short-return percentages;
- first observed -100% / -200% / -300% / -400% adverse breach times;
- intuitive visual outcomes for target-hit, profitable-below-target, negative-but-unbreached, pending and breach severity;
- a CSV attachment containing the complete raw ledger for filtering and offline analysis.

The ledger is on-demand only by default and does not alter the existing scheduled performance board. No database migration is required.

## v1.1.10 — compact visual signal ledger

The on-demand signal outcome ledger keeps the CSV export but replaces the verbose per-token Discord embed fields with compact PNG table pages, split by STANDARD, HIGH RISK and EXTREME RISK.

Run:

```bash
python -m app.signal_ledger_now
```

Each table row shows signal time/price, +20% target timing, 1D/2D/3D/7D price + short return, and first -100/-200/-300/-400 adverse-breach times. Color semantics: green = profitable/target, amber = negative but not liquidated at -100%, red = liquidation-type breach already occurred, blue = pending. The full exact ledger remains attached as CSV.


## v1.2.0 — multi-slot runner trader + live execution safety + Discord operations

The trader is now a configurable portfolio engine. The default paper strategy is the current Strategy 1 experiment: STANDARD + HIGH_RISK, cross model, 1x, six slots, approximately 3.33% notional per slot, 20% aggregate exposure cap, and at most five HIGH_RISK positions so STANDARD can retain capacity. EXTREME_RISK is excluded by default.

`+20%` is now a milestone rather than an exit. At +25% peak short return the trader arms profit protection. It protects approximately +20% gross return and then ratchets the floor upward with a 15% price-retracement rule while the short continues to run. Paper mode emulates the stop locally; live mode places and modifies a position-level protection stop at MEXC and periodically verifies that the exchange-side protection still exists.

Key configuration:

```text
TRADING_MODE=paper
TRADER_MARGIN_MODE=cross
TRADER_LEVERAGE=1
TRADER_ALLOWED_RISK_TIERS=STANDARD,HIGH_RISK
TRADER_MAX_OPEN_POSITIONS=6
TRADER_MAX_TOTAL_EXPOSURE_PCT=20
TRADER_MAX_HIGH_RISK_POSITIONS=5
TRADER_ALLOW_SAME_SYMBOL_PARALLEL=false
TRADER_PROFIT_TARGET_PCT=20
TRADER_PROTECTION_ARM_PCT=25
TRADER_TRAIL_CALLBACK_PCT=15
DISCORD_TRADER_EVENTS_WEBHOOK_URL=...
```

`TRADER_SLOT_ALLOCATION_PCT` is optional; when omitted it is calculated as max total exposure divided by the configured number of slots. The HIGH cap also defaults dynamically: it reserves one STANDARD slot only when STANDARD and HIGH_RISK are both enabled.

Trader-event Discord is intentionally selective: it sends position opens, the first +20% profit milestone, each first-time cumulative -100%/-200%/-300%/-400% adverse breach, position closes/exchange-side exits, and server/API/live-safety errors. Skip decisions, protection-arm/ratchet updates, startup/shutdown and routine heartbeats remain in Render logs/PostgreSQL only. Discord event messages include portfolio equity/MTM when available, slot/exposure usage, performance totals and current open positions. The scanner independently watches the trader database heartbeat and alerts if the trader process hard-crashes.

Live mode uses the current MEXC Futures API and remains fail-closed until credentials and explicit live gates are configured. Run the read-only preflight before flipping live:

```bash
python -m app.trader_preflight
```

Test the trader-events Discord channel:

```bash
python -m app.trader_notify_test
```

See `TRADER-DEPLOY.md` for the full Render configuration and live checklist. Migration `012_multi_slot_live_trader.sql` is applied automatically.

## v1.2.1 operational audit hotfix

Every confirmed-short signal consumed by the trader now produces an explicit audit decision. Opened positions are logged as `OPENED`; non-traded signals are persisted, logged at INFO, and sent to `DISCORD_TRADER_EVENTS_WEBHOOK_URL` with the exact reason and current portfolio snapshot. Skip reasons include risk-tier filtering, stale signal, reconciliation halt, slot capacity, duplicate symbol, reserved STANDARD capacity, and aggregate exposure cap. Position closes are also logged at INFO.





## v1.3.1 — tier-specific live strategy

Promotes the exit/capacity structure selected from the v1.2.9 research into the trader while keeping the new entry-quality model in shadow.

- Portfolio capacity is now **5 STANDARD + 1 HIGH_RISK** inside the existing 6-slot / 20% aggregate exposure model. Default slot size remains ~3.3333% of equity at 1x cross.
- New STANDARD positions use `fixed_time_standard`: enter immediately on confirmation, treat +20% as telemetry only, and close the full position at **7 days**. No runner/trailing protection is applied to these new positions.
- New HIGH_RISK positions use `tp20_or_timeout`: close the full position at **+20% short return**, otherwise close at **4 days**.
- Conventional tight stops remain disabled; -100/-200/-300/-400% adverse levels remain cumulative telemetry/alerts.
- Existing positions opened before v1.3.1 keep their persisted legacy runner/protection strategy so deployment does not mutate a live trade mid-position.
- Entry timing remains immediate. `entry_quality` and `continuation_risk` remain frozen shadow diagnostics only; they do not filter signals yet.
- HIGH_RISK research timeouts from 1d through 10d now use the **same paired 10-day cohort**, allowing a fair 4d-vs-longer validation.
- Adds migration `014_tier_exit_strategy.sql` for the new persisted exit-strategy values and maturities.

Recommended trader defaults:

```text
TRADER_MAX_OPEN_POSITIONS=6
TRADER_MAX_TOTAL_EXPOSURE_PCT=20
TRADER_MAX_STANDARD_POSITIONS=5
TRADER_MAX_HIGH_RISK_POSITIONS=1
TRADER_STANDARD_HOLD_DAYS=7
TRADER_HIGH_RISK_TIMEOUT_DAYS=4
TRADER_PROFIT_TARGET_PCT=20
```

See `TRADER-DEPLOY.md` for the deployment/live checklist.

## v1.2.9 — paired-cohort analytics corrections

Corrects the comparison biases exposed by the first v1.2.8 report. Live scanner and trader rules remain unchanged.

- STANDARD 1d–7d exit horizons now use the **same complete-7d cohort**. Extended 8d–14d horizons use the same complete-14d cohort, so horizon comparisons no longer change denominator as signals mature.
- HIGH RISK `TP20 or timeout` now requires a signal to have **actually reached the timeout age** before it is eligible. An early +20% hit can no longer make a young signal appear as a 10d/14d winner.
- HIGH RISK exit rows now report **average actual holding time** and **return per occupied slot-day** (`sum(strategy returns) / sum(holding days)`) to compare capital efficiency as well as raw return.
- Delayed-entry simulations now use one **common complete cohort across every configured delay** (0m through 8h), making the timing rows directly comparable.
- CSV strategy exports mark paired/mature-only analyses and include cohort horizon, average holding hours, and slot-day efficiency.
- No MEXC/API calls, migrations, scanner filters, slot allocation, or trader exit behavior are changed.

Run:

```bash
python -m app.research_analytics_now
```

## v1.2.8 — entry/exit research lab

Extends the v1.2.7 analytics into a research-only strategy lab. Live scanner and trader rules are still unchanged.

Run from the scanner Render shell:

```bash
python -m app.research_analytics_now
```

New research capabilities:

- Post-signal 15m path collection uses **336h / 14 days as the minimum fixed research horizon** (`RESEARCH_PATH_HORIZON_HOURS=336`). If TP5 remains unresolved after that point, collection continues until +5% is observed; fixed 7d/14d statistics remain explicitly bounded to their own horizons.
- Complete-path classification now requires the expected end timestamp and, for 7d/14d, at least **98% 15m candle coverage**. Exact signal-close candles are excluded from post-entry excursions to avoid pre-entry look-ahead.
- STANDARD fixed-time exit sweep: **1d, 2d, 3d, 4d, 5d, 6d, 7d, 8d, 10d, 12d, 14d**, reporting sample, positive rate, average/median/worst/best return and average return per day of slot occupation.
- HIGH RISK strategy sweep: **+20% TP first, otherwise timeout** at 1d/2d/3d/4d/5d/7d/10d/14d.
- Winner stop-survival analysis at adverse **10/20/30/50/75/100%** thresholds to quantify how many eventual +20% winners a hypothetical stop would have killed first.
- Two frozen, reproducible **shadow scores** derived from the first v1.2.7 evidence sample: `entry_quality` and `continuation_risk`. They are diagnostics only and do not gate signals.
- Feature-interaction research for the highest-priority pairs, including exhaustion×volume, exhaustion×funding, run-score×volume, turnover×volume, premium×funding, pump×momentum and 72h-run×run-score.
- Delayed-entry simulations at **0m, 15m, 30m, 1h, 2h, 4h and 8h**, using the first 15m close at/after the delay and then a fresh seven-day path from that delayed entry. The entry candle's earlier high/low is excluded.
- Discord now adds exit-research, stop-survival and shadow-entry cards, plus two additional CSVs for strategy sweeps and entry research.
- The heavier delayed-entry SQL has its own research statement timeout and fails independently; the rest of the report is still sent if that optional analysis times out.

No MEXC/API calls are added. All new analytics use frozen features and candles already stored in PostgreSQL.

## v1.2.7 — research analytics & feature lift

Builds actionable research analytics on top of the v1.2.6 frozen feature snapshots and 15m post-signal paths. No scanner thresholds or trader behavior change automatically.

Run from the scanner Render shell:

```bash
python -m app.research_analytics_now
```

The on-demand report posts to the performance Discord webhook and includes:

- sample size, 7-day maturity and complete-path coverage so incomplete research data is visible rather than silently treated as failure;
- baseline +20% hit rate within seven days, exact 7-day profitability, average/median 7-day short return, median MFE/MAE, median adverse excursion before a successful +20% hit, and timing of MFE/MAE;
- a 7-day favorable-excursion sweep for +5%, +10%, +15%, +20%, +25%, +30% and +40%, including hit rate, median time-to-hit and p75 time-to-hit;
- univariate feature-lift analysis across the frozen run/exhaustion features plus episode/breakdown/retest timing. Numeric features are split into sample tertiles and booleans into TRUE/FALSE groups;
- strongest and weakest candidate feature slices ranked only after a minimum sample guard (`max(3, 15% of matured signals)`);
- two CSV attachments: a full flattened per-signal research dataset and the complete feature-bucket lift table for offline analysis.

The feature-lift board is deliberately exploratory. It is intended to identify hypotheses for the next strategy iteration, not to mutate production filters from a small or correlated sample. The command uses PostgreSQL data only and performs no MEXC API calls.

## v1.2.6 — low-impact research logging

- Adds internal `research_signal_features` snapshots and a bounded `research_signal_path_15m` dataset for strategy research.
- The research layer uses only candles/signals already stored in PostgreSQL; it makes **no additional MEXC API calls** and does not add writes to the confirmed-signal hot path.
- A separate research loop runs every 15 minutes by default, upserts missing feature snapshots, and copies at most 2,000 15m path rows per cycle.
- Research path collection defaults to the first 168h after each confirmed signal and records raw OHLCV/amount, close return, per-candle favorable/adverse excursion, and the matching stored BTC 15m close.
- `research_signal_features_enriched` derives run/breakdown/retest/confirmation timing, while `research_signal_path_15m_enriched` derives cumulative MFE/MAE, best/worst close return, giveback from best, rebound from worst, minutes since signal, and BTC return since signal only when queried.
- Research SQL uses its own 10-second PostgreSQL statement timeout so optional backfill cannot monopolize the scanner DB pool. A failed research cycle is isolated by the existing periodic-loop error handling.
- Defaults: `RESEARCH_LOGGING_ENABLED=true`, `RESEARCH_PATH_POLL_SECONDS=900`, `RESEARCH_PATH_BATCH_ROWS=2000`, `RESEARCH_PATH_HORIZON_HOURS=336`, `RESEARCH_DB_TIMEOUT_SECONDS=10`.
- Run `python -m app.research_status` to inspect snapshot/path row counts.
- Public Discord reporting, signal rules, episode locking/re-arm behavior, performance tracking, and trader strategy are unchanged. Migration `013_research_signal_paths.sql` is applied automatically.

## v1.2.5 — concurrent signal evaluation + progress diagnostics

- Signal evaluation now processes symbols with controlled concurrency instead of strictly sequential symbol-by-symbol database reads.
- `SIGNAL_EVAL_CONCURRENCY` defaults to `3`. Each symbol initially reads 15m and 4h candles in parallel, so three active symbols use at most about six of the scanner PostgreSQL pool’s eight connections and leave headroom for ticker/performance/other worker tasks.
- Every cycle logs `Signal evaluation started`, periodic `Signal evaluation progress`, and `Signal evaluation complete` with failures and wall-clock duration.
- `SIGNAL_EVAL_PROGRESS_EVERY` defaults to `50` symbols.
- A failure on one symbol is logged and isolated instead of aborting the entire evaluation cycle.
- Signal rules, pump-episode locking/re-arm logic, Discord filtering, performance tracking and trader behavior are unchanged.

## v1.2.4 — neutral public outcomes + STD/HIGH-only tracking

- Public performance and signal ledger datasets include only `standard` and `high_risk`. `extreme_risk` remains stored internally by the scanner but is excluded from public counts, CSVs, tables, MTM summaries, horizon returns, excursions, and Discord performance cards.
- Fixed 1D/2D/3D/7D sections are raw outcome summaries rather than hypothetical stop-loss strategies. Every matured return contributes to Avg raw and Σ raw, including negative returns and signals that crossed adverse thresholds.
- Adverse -100/-200/-300/-400 counts are shown separately as path observations and may overlap with profitable/not-profitable outcomes.
- The horizon-independent +20% section reports target-first rate, target/breach/pending counts, and average target time only; it no longer reports synthetic +20% Avg/Σ profit.
- Discord performance cards are now `STANDARD Signal Outcomes` and `HIGH RISK Signal Outcomes`, with neutral methodology wording and no EXTREME card.

## v1.2.3 — trader Discord milestones

Restores concise trader-event Discord milestones without bringing back routine noise. The trader now sends one alert when a position first reaches +20% and one alert for each first-time cumulative adverse threshold crossed at -100%, -200%, -300% and -400%. Each alert includes the triggering price/P&L plus the current portfolio snapshot. Skip/filter decisions, protection-ratchet updates and routine heartbeats remain log/DB only.

## v1.2.2 — quiet trader Discord

Discord is restricted to OPEN / CLOSE / ERROR events. All other trader decisions and milestones remain fully logged in Render/PostgreSQL without Discord noise. No migration is required.

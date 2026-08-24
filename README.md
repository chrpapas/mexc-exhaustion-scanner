# MEXC Exhaustion Scanner + Multi-Slot Futures Trader v1.3.11

Research-only token-behaviour release. **TP5_V1 execution is unchanged from v1.3.6.** The new work tests the original niche hypothesis: isolated/episodic pumps may be better short-exhaustion candidates than tokens whose normal price action is mostly explained by the broader crypto regime.

**v1.3.11 research reporting:** adds fast-target/high-capacity shadow challengers alongside the frozen TP5 control: **TP2-10** (full +2% exit, 10 generic slots × 5% equity, 50% cap) and **TP1-10** (full +1% exit with the same 10×5% / 50% cap). The prior TP2-6 replay is retained as a bridge baseline. Calendar throughput and future true-30d replay compare return, drawdown, holding time, slot-days, exposure, releases and capacity misses on the same signal stream. No scanner or trader execution rule changes.

**v1.3.8 hotfix:** the bounded 15m research-path catch-up now limits the episode set before sorting/joining candles and uses index-friendly `open_time` predicates. The on-demand analytics command also continues from already persisted paths if this optional catch-up hits its local PostgreSQL statement timeout.

- **Frozen trader unchanged:** 6 generic STANDARD/HIGH_RISK slots, 5% of current equity each, 30% aggregate cap, 1x cross, immediate entry, one open position per symbol, full +5% exit. EXTREME_RISK remains excluded.
- **90-day pre-signal behaviour profile:** research uses completed 4h token/BTC candles strictly before each signal; no post-signal candles enter the classifier.
- **Three unsupervised components:** positive BTC explanatory power (`market_r2`), concentration of positive movement in the five largest 4h gains, and frequency of isolated 4h pumps of at least +5% that outperform BTC by at least 4 percentage points.
- **Frozen discovery calibration:** the components are converted to empirical ranks using only the pre-freeze discovery cohort (`2026-08-21 21:29 UTC`). TP5 outcomes are not used to set the behaviour buckets. The resulting score is split into `REGIME_FOLLOWER`, `MIXED`, and `EPISODIC`; incomplete histories remain `INSUFFICIENT`.
- **Shadow portfolio comparison:** the report now shows TP5-All against `tp5_no_regime_followers`, `tp5_episodic_only`, and `tp5_episodic_priority_same_bar`. The priority variant only reorders signals sharing the same confirmation timestamp; it never replaces an already-open position.
- **Conservative handling of new coins:** the `no_regime_followers` variant rejects only confidently classified regime followers. `INSUFFICIENT` history is still accepted so newly listed pumpers are not automatically excluded.
- **Historical coverage fix:** v1.3.6 could seed a symbol with four days of 4h candles during the wide scan and then fail to backfill the older 120-day window. v1.3.8 checks the earliest candle and fills the missing left edge.
- **Research-history backfill:** while research logging is enabled, the scanner periodically ensures the 90-day pre-signal 4h history exists for every stored public signal symbol plus BTC. `RESEARCH_REGIME_HISTORY_POLL_SECONDS` defaults to 21600 (6h).
- **New Discord/CSV output:** `Token Behaviour • Regime Dependency` plus `research-token-regime-YYYY-MM-DD.csv`.
- **No schema migration:** v1.3.11 derives +1%/+2% target timestamps from the existing stored 15m research paths. Migration `015_tp5_trader_runs.sql` remains the latest migration.

The paper-run isolation, configurable `$2,000` default starting equity, and fail-closed live-account handling from v1.3.6 are unchanged. The legacy `tier_v1` path remains only for rollback/persisted compatibility.

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

- Post-signal 15m path collection now defaults to **336h / 14 days** (`RESEARCH_PATH_HORIZON_HOURS=336`). Existing 7-day MFE/MAE and target statistics remain explicitly bounded to the first 168h, so extending storage cannot contaminate the old baseline.
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
- Defaults: `RESEARCH_LOGGING_ENABLED=true`, `RESEARCH_PATH_POLL_SECONDS=900`, `RESEARCH_PATH_BATCH_ROWS=2000`, `RESEARCH_PATH_HORIZON_HOURS=168`, `RESEARCH_DB_TIMEOUT_SECONDS=10`.
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

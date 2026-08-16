# MEXC Exhaustion Scanner + STANDARD Short Trader v1.0.0

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

## v1.0.0 — STANDARD short trader (paper first)

This repository now contains a second Render worker, `mexc-standard-short-trader`, which consumes the scanner's persisted `confirmed_short` signals from the same PostgreSQL database.

### Trading rules

- Only `STANDARD` execution-risk `confirmed_short` signals are eligible.
- Exactly one trader position may be open at a time. Any later confirmed signal is permanently recorded as `ignored_busy` while that position is open.
- The default start mode is `TRADING_MODE=paper`; no MEXC API key is needed for paper mode.
- Paper entry uses the current MEXC futures last price when the trader processes the new signal, not the historical scanner retest close.
- Exposure is always modeled at 1x.
- `TRADER_CAPITAL_STRATEGY=isolated_full`: notional = 100% of current account equity, with a configurable +100% adverse-move liquidation research proxy.
- `TRADER_CAPITAL_STRATEGY=cross_20`: notional = 20% of current account equity, with a configurable +400% adverse-move cross-buffer research proxy.
- Paper account P&L compounds after each closed/liquidated paper position.

### Position maturity (current behavior)

The v1.0 ratchet/trailing experiment has been superseded by a simpler maturity setting. The trader now has three independent configuration axes: trading mode, capital/margin model, and position maturity.

```text
TRADING_MODE=paper                         # paper | live
TRADER_CAPITAL_STRATEGY=cross_20          # cross_20 | isolated_full
TRADER_POSITION_MATURITY=profit_20         # profit_20 | 1d | 2d | 3d | 7d
TRADER_PROFIT_TARGET_PCT=20
PAPER_STARTING_EQUITY_USDT=2000
TRADER_POLL_SECONDS=5
TRADER_PROCESS_EXISTING_SIGNALS=false
TRADER_MAX_SIGNAL_AGE_SECONDS=900
TRADER_ISOLATED_ADVERSE_LIMIT_PCT=100
TRADER_CROSS_ADVERSE_LIMIT_PCT=400
DISCORD_TRADER_WEBHOOK_URL=                 # optional paper/live trade-event channel
```

- `profit_20`: close at the first observed +20% short return (or configured `TRADER_PROFIT_TARGET_PCT`).
- `1d`, `2d`, `3d`, `7d`: hold until that elapsed maturity and close at market at the horizon.
- In paper mode, the selected liquidation research proxy closes the position earlier if breached.
- Exactly one position can be open. New signals while busy are recorded as ignored and are not replayed.

`TRADER_PROCESS_EXISTING_SIGNALS=false` is intentional: on the first trader start, the cursor is initialized to the newest already-stored confirmed-short signal, so an old signal cannot accidentally open a new position.

### Live mode safety gate

The live adapter contains MEXC Contract API authentication, USDT-equity queries, contract sizing, open-position queries, market short submission and market close submission. However, MEXC's current public Contract API documentation labels the order mutation endpoints as under maintenance. For that reason live mode is fail-closed.

To arm live mode in a future environment where your MEXC account has verified futures order API access, all of these are required:

```text
TRADING_MODE=live
MEXC_API_KEY=...
MEXC_API_SECRET=...
MEXC_LIVE_ORDER_API_ENABLED=true
LIVE_TRADING_CONFIRM=I_UNDERSTAND_LIVE_TRADING
```

Do not set these flags merely to test the worker. Use `paper` until live futures order access is independently verified on the account.

### Database

Migration `009_trader_bot.sql` adds:

- `trader_runtime`
- `trader_positions`
- `trader_signal_decisions`
- `trader_position_events`

A partial unique index guarantees one open trader position at database level. Schema migrations are also protected by a PostgreSQL advisory lock because the scanner and trader workers can deploy simultaneously.

### Render Shell status

```bash
python -m app.trader_status
```

This prints paper equity, the signal cursor and the active position's entry/current return/peak/adverse excursion/profit floor.

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

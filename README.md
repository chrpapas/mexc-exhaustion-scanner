# MEXC Exhaustion Scanner v0.8.7

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

By default Discord receives only:

- `EXHAUSTION WATCH` — the prior run is showing exhaustion.
- `CONFIRMED SHORT` — a breakdown was followed by a failed retest.

`RUN WATCH` and `BREAKDOWN WATCH` continue to be stored and processed internally, but they are not posted to Discord. The daily performance report is unchanged. Configure this with `DISCORD_SIGNAL_LEVELS` if you ever want different alert levels.

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

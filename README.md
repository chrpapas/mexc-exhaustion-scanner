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

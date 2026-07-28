# MEXC Post-Run Exhaustion Scanner — Clean Online Build

This repository runs as a **Render background worker** and stores data in **Render PostgreSQL**. It does not need your computer after deployment.

This build is deliberately **shadow mode only**:

- No MEXC API key.
- No order placement.
- No leverage or position management.
- It records MEXC perpetual-futures market data and alerts on abnormal runs.

## What it collects

- Active MEXC USDT perpetual contracts.
- Five-minute snapshots for the most liquid contracts: last price, bid/ask spread, 24-hour amount, funding, fair/index prices and `holdVol`.
- 15-minute and four-hour candles.
- Recent funding-rate history.
- Candidate/watch signals and a worker heartbeat.

## Candidate score

Liquidity and spread are mandatory. One point is awarded for each:

1. 24-hour return at least 20%.
2. 72-hour return at least 30%.
3. 24-hour return minus BTC return at least 15%.
4. Return at or above the 95th percentile of the eligible MEXC universe.
5. Latest completed 15-minute volume z-score at least 2.5.
6. Price at least 2.5 ATR above the completed four-hour EMA20.

A score of 4 is stored as a watch. A score of 5 or 6 is stored as a candidate and can be sent to Discord. This is a **run detector**, not yet the final peak/exhaustion entry model.

## Deploy on Render

1. Create a fresh private GitHub repository.
2. Extract this ZIP and upload the extracted files and folders to the repository root.
3. Confirm that `render.yaml` is visible at the repository root.
4. In Render, choose **New → Blueprint** and select the repository.
5. Use Blueprint path `render.yaml`.
6. Apply the Blueprint.
7. To enable alerts later, open the worker’s **Environment** page and add `DISCORD_WEBHOOK_URL`.
8. Keep `EXECUTION_ENABLED=false`.

Render creates:

- `mexc-exhaustion-scanner` — Python background worker in Frankfurt.
- `mexc-exhaustion-db` — PostgreSQL in Frankfurt.

This clean build uses Render's native Python runtime, so there is **no Dockerfile, Makefile, Redis or local Docker setup to confuse the deployment**.

## Expected logs

```text
Database connected and migrations applied
Refreshed contracts: ... active USDT perpetuals
Ticker refresh: received=... stored=...
Candle sync complete: symbols=... failures=...
Funding sync complete: symbols=... failures=...
Signal evaluation: evaluated=... candidates=...
```

The first candle backfill can take several minutes because it requests two timeframes for many symbols while respecting MEXC rate limits.

## Verify repository contents

Run this in GitHub Codespaces or any Python 3.12 environment:

```bash
python scripts/verify_project.py
```

The script detects the exact filename/content mix-up that happened in the previous repository.

## Tests

```bash
pip install -e '.[dev]'
pytest
```

## Important environment variables

| Variable | Default | Meaning |
|---|---:|---|
| `MIN_AMOUNT_24H` | `10000000` | Minimum MEXC 24-hour transaction amount in USDT |
| `MAX_SPREAD_PCT` | `0.25` | Maximum bid/ask spread percentage |
| `MAX_SYMBOLS` | `250` | Maximum liquid contracts stored and evaluated |
| `TICKER_STORE_SECONDS` | `300` | Ticker/OI snapshot interval |
| `MIN_RUN_SCORE` | `5` | Discord candidate threshold |
| `WATCH_RUN_SCORE` | `4` | Database watch threshold |
| `EXCLUDED_SYMBOLS` | `BTC_USDT,ETH_USDT` | Symbols not alerted as altcoin candidates |

## Database checks

From Render's PostgreSQL console:

```sql
SELECT * FROM worker_heartbeat;
SELECT count(*) FROM contracts;
SELECT count(*) FROM ticker_snapshots;
SELECT interval, count(*) FROM candles GROUP BY interval;
SELECT symbol, signaled_at, level, score, reasons
FROM run_signals
ORDER BY signaled_at DESC
LIMIT 20;
```

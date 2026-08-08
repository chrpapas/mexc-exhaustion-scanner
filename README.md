# MEXC Post-Run Exhaustion Scanner — v0.6

This repository runs as a **Render background worker** with **Render PostgreSQL**. It is shadow-mode only: it does not place orders and does not require a MEXC trading API key.

## Signal state machine

v0.6 adds persistent pump episodes and removes the old behavior where a structural break immediately produced a short alert.

1. **RUN WATCH** — an abnormal move is still advancing.
2. **EXHAUSTION WATCH** — a large prior run is fading or showing intraday reversal evidence.
3. **BREAKDOWN WATCH** — a completed 15-minute candle has broken recent support with enough exhaustion evidence. The broken level and ATR are stored persistently.
4. **CONFIRMED SHORT** — a later completed 15-minute candle retests the stored broken level from below and rejects it.

A structural break alone is **not** a confirmed short anymore.

## Failed-retest confirmation

Default rules after BREAKDOWN WATCH:

- The broken level is the minimum low of the four completed 15m candles before the breakdown candle.
- The scanner waits up to **6 completed 15m candles** (90 minutes).
- A retest must trade to within **0.5 × the saved 15m ATR** of the broken level.
- The retest candle must close below the broken level and close bearish.
- A completed candle that closes back above the broken level invalidates that breakdown attempt.
- If no qualifying retest occurs within the window, the breakdown attempt expires without a short signal.

## Persistent pump episodes

Every watched symbol is assigned a PostgreSQL `pump_episodes` record containing:

- episode id and start time
- state
- episode peak and peak time
- broken support level
- breakdown time and saved 15m ATR
- retest time
- confirmed-short time
- latest run/exhaustion scores

Only **one CONFIRMED SHORT is allowed per episode**.

Once confirmed, the episode is locked. A new episode can re-arm only after a later completed candle establishes a new high at least **5% above the prior episode peak**, while the normal run filters are active again. Episodes also expire after 240 hours by default so stale historical pumps do not remain open forever.

## Run score (6 points)

Liquidity and spread are mandatory. One point is awarded for each:

1. 24h return >= 12%.
2. 72h return >= 20%.
3. 24h return minus BTC 24h return >= 10%.
4. Return at or above the 90th percentile of the eligible MEXC crypto universe.
5. Latest completed 15m volume z-score >= 1.5.
6. Price >= 1.5 ATR above the completed 4h EMA20.

Default discovery universe:

- MEXC USDT perpetual.
- Backed by an active MEXC USDT spot asset.
- 24h futures amount >= 3M USDT.
- Bid/ask spread <= 0.35%.
- BTC and ETH are collected as context but excluded from altcoin alerts by default.

## Exhaustion score (7 points)

One point each for:

1. 15m upper wick >= 35% of candle range.
2. 15m close in the bottom 45% of candle range.
3. 1h momentum decelerating versus the prior hour.
4. 15m close below EMA9.
5. Lower high plus lower close.
6. 15m structural support break.
7. 15m volume z-score >= 1.25.

BREAKDOWN WATCH requires EXHAUSTION WATCH, a structural break, and exhaustion score >= 3/7.

## Expected Discord alerts

```text
🟡 XYZ_USDT — RUN WATCH
Episode: #123
...

🟠 XYZ_USDT — EXHAUSTION WATCH
Episode: #123
...

🔴 XYZ_USDT — BREAKDOWN WATCH
Episode: #123
Broken level: ...
Retest window: 6 × 15m candles
Retest tolerance: 0.50 ATR
...

🚨 XYZ_USDT — CONFIRMED SHORT
Episode: #123
Broken level: ...
Retest high: ...
Retest close: ...
Episode locked: YES — no second short alert unless a new episode re-arms
...
```

All alerts state that shadow mode is active and no order is placed.

## Expected logs

```text
Database connected and migrations applied
Refreshed contracts: total=... active_usdt=... crypto=... excluded_non_crypto=...
Ticker refresh: received=... crypto=... stored=...
Candle sync complete: symbols=... failures=...
Funding sync complete: symbols=... failures=...
Signal evaluation: evaluated=... run_watches=... exhaustion_watches=... breakdown_watches=... breakdown_waiting=... confirmed_shorts=... rearmed=...
```

## Updating an existing Render deployment

Replace the changed files in GitHub and add:

```text
migrations/004_pump_episodes.sql
```

Commit to `main`. Render redeploys automatically and applies migration 004 once through the `schema_migrations` table.

Migration 004 preserves existing `run_signals`, creates `pump_episodes`, links future signals to an `episode_id`, and extends allowed signal levels with `breakdown_watch` and `confirmed_short`.

## Important environment variables

| Variable | Default |
|---|---:|
| `MIN_AMOUNT_24H` | `3000000` |
| `MAX_SPREAD_PCT` | `0.35` |
| `STATE_MIN_RUN_SCORE` | `3` |
| `RUN_WATCH_MIN_24H` | `0.08` |
| `RUN_WATCH_MIN_72H` | `0.20` |
| `EXHAUSTION_WATCH_MIN_72H` | `0.30` |
| `EXHAUSTION_WATCH_MIN_24H` | `-0.05` |
| `EXHAUSTION_WATCH_MAX_24H` | `0.08` |
| `ACTIVE_EXHAUSTION_MIN_SCORE` | `2` |
| `SHORT_EXHAUSTION_SCORE` | `3` |
| `RETEST_WINDOW_CANDLES` | `6` |
| `RETEST_TOLERANCE_ATR` | `0.5` |
| `REARM_NEW_HIGH_PCT` | `0.05` |
| `EPISODE_MAX_AGE_HOURS` | `240` |
| `SIGNAL_POLL_SECONDS` | `300` |

## Tests

```bash
pip install -e '.[dev]'
pytest
```

v0.6 passes the existing HEI/CYS/BICO state-classification regression tests plus new tests for successful failed-retest confirmation, breakdown invalidation, and retest-window expiry.

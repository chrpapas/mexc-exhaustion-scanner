# MEXC Post-Run Exhaustion Scanner — v0.8

Render-hosted, MEXC-only, shadow-mode scanner for post-run exhaustion shorts. It does not place orders.

## v0.8: risk-aware discovery

Liquidity is no longer a discovery gate. The scanner can surface thin/illiquid runners while clearly separating signal quality from execution quality.

Every alert receives one of three execution-risk tiers:

- `STANDARD`: 24h futures turnover >= $3M and spread <= 0.35%
- `HIGH RISK`: below the standard threshold, but turnover >= $500k and spread <= 1.00%
- `EXTREME RISK`: thinner/wider than the high-risk tier, or spread is unavailable

`HIGH RISK` and `EXTREME RISK` signals are still evaluated through the same run -> exhaustion -> breakdown -> failed-retest pipeline. Discord prints an explicit warning, current 24h turnover, spread, and the failed execution-quality conditions.

### Discovery universe

The worker does not download full candle history for every dormant MEXC contract. A crypto perpetual is selected for full analysis when any of these are true:

- it meets standard execution-quality liquidity,
- its current 24h return is at least +5%,
- it ranks in the top 30% of the current MEXC crypto cross-section,
- it already has an active pump episode.

Active episodes are always retained even if their return collapses or liquidity deteriorates. `MAX_SYMBOLS` defaults to 400, with active episodes exempt from the cap.

This means low-liquidity pumps such as CASHCAT-style moves can become visible instead of being silently discarded by the $3M gate.

## Signal state machine

1. `RUN WATCH`
2. `EXHAUSTION WATCH`
3. `BREAKDOWN WATCH`
4. `CONFIRMED SHORT` only after a later failed retest of the saved broken support level.

One confirmed short is allowed per pump episode. A confirmed episode can only re-arm after a materially higher new high.

## Discord risk warning example

```text
🚨 XYZ_USDT — CONFIRMED SHORT
⚠️ HIGH-RISK / LOW-LIQUIDITY CANDIDATE
Execution-quality filter: FAIL — signal remains visible for research.
24h futures turnover: $1.20M
Bid/ask spread: 0.62%
...
Risk flags: 24h turnover below standard $3,000,000; spread above standard 0.35%
Shadow mode only — no order is placed.
```

For an extreme-risk contract the warning changes to:

```text
⛔ EXTREME EXECUTION RISK
Analytics only — thin liquidity/spread can make this impractical to short safely.
```

## Daily performance tracker

Every `CONFIRMED SHORT` creates a shadow trade at the failed-retest candle close. The tracker stores:

- current mark-to-market short return
- maximum favorable excursion (MFE) during the first 24h
- maximum adverse excursion (MAE) during the first 24h
- 1h, 4h, 12h and 24h short returns
- 24h win/loss outcome
- execution-risk tier at confirmation

Once per day the worker posts a Discord summary at 18:00 Europe/Zurich by default. The report now also separates the 24h win rate for `STANDARD` signals from the combined `HIGH RISK + EXTREME RISK` group.

These are signal analytics, not account P&L. Fees, slippage, funding, leverage and position sizing are deliberately excluded.

## New migration

Keep migrations `001` through `005` and add:

`migrations/006_risk_tiers.sql`

Migrations are tracked in `schema_migrations` and run once.

## Key environment variables

| Variable | Default |
|---|---:|
| `MIN_AMOUNT_24H` | `3000000` |
| `MAX_SPREAD_PCT` | `0.35` |
| `HIGH_RISK_MIN_AMOUNT_24H` | `500000` |
| `HIGH_RISK_MAX_SPREAD_PCT` | `1.0` |
| `DISCOVERY_MIN_RETURN_24H` | `0.05` |
| `DISCOVERY_MIN_CROSS_SECTION_PERCENTILE` | `0.70` |
| `MAX_SYMBOLS` | `400` |
| `PERFORMANCE_REPORT_HOUR` | `18` |
| `PERFORMANCE_REPORT_TIMEZONE` | `Europe/Zurich` |

## Tests

```bash
pip install -e '.[dev]'
pytest
python scripts/verify_project.py
```

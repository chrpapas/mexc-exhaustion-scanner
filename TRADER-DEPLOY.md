# Deploying the v1.1.1 trader on Render

The repository contains two workers:

- `mexc-exhaustion-scanner`: discovers and publishes confirmed-short signals and performance statistics.
- `mexc-standard-short-trader`: consumes only new STANDARD confirmed-short signals and maintains at most one position.

## Three independent trader settings

### 1. Trading mode

```text
TRADING_MODE=paper
```

or, only after live futures API access has been explicitly verified and armed:

```text
TRADING_MODE=live
```

### 2. Margin / capital model

Full-account 1x isolated research model:

```text
TRADER_CAPITAL_STRATEGY=isolated_full
```

20%-of-equity 1x cross research model:

```text
TRADER_CAPITAL_STRATEGY=cross_20
```

### 3. Position maturity

Exit at the first observed +20% short return:

```text
TRADER_POSITION_MATURITY=profit_20
TRADER_PROFIT_TARGET_PCT=20
```

Or hold until a fixed maturity and close at market at that time:

```text
TRADER_POSITION_MATURITY=1d
TRADER_POSITION_MATURITY=2d
TRADER_POSITION_MATURITY=3d
TRADER_POSITION_MATURITY=7d
```

A paper position closes earlier if its selected liquidation research proxy is breached. In live mode the exchange determines actual liquidation.

## Deployment

1. Deploy the updated Blueprint/repository.
2. Both workers use the same `mexc-exhaustion-db`.
3. Migration `010_position_maturity.sql` is applied automatically.
4. Keep `TRADING_MODE=paper` initially.
5. Default configuration is `cross_20` + `profit_20` with $2,000 starting paper equity.
6. Optional: set `DISCORD_TRADER_WEBHOOK_URL` for private trader execution events.
7. Inspect the trader with `python -m app.trader_status` in the trader Render Shell.

`TRADER_PROCESS_EXISTING_SIGNALS=false` remains the safe default, so a fresh deployment starts with future STANDARD signals rather than replaying old history.

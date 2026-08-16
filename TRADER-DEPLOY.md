# Trader deployment — v1.2.0

The existing Render trader service (`mexc-standard-short-trader`; name kept for deployment continuity) is now a configurable multi-slot portfolio trader. It reads confirmed-short signals from the same PostgreSQL database as the scanner.

## Default paper strategy

- `TRADING_MODE=paper`
- STANDARD + HIGH_RISK only; EXTREME_RISK excluded
- cross margin model, 1x leverage
- 6 slots
- 20% maximum aggregate initial notional exposure
- slot size defaults to `max exposure / slots` (3.3333% with the default 6/20 configuration)
- at most 5 HIGH_RISK positions, preserving capacity for STANDARD
- no concurrent duplicate symbol
- +20% is a milestone, not an exit
- protection arms at +25%; then the bot protects at least approximately +20% gross return and ratchets the floor using a 15% price-retracement trail
- paper taker fee default: 0.08% per fill

## Recommended Render environment

```text
MEXC_BASE_URL=https://api.mexc.com
MEXC_WS_URL=wss://contract.mexc.com/edge
TRADING_MODE=paper
TRADER_MARGIN_MODE=cross
TRADER_LEVERAGE=1
TRADER_ALLOWED_RISK_TIERS=STANDARD,HIGH_RISK
TRADER_MAX_OPEN_POSITIONS=6
TRADER_MAX_TOTAL_EXPOSURE_PCT=20
TRADER_MAX_HIGH_RISK_POSITIONS=5
TRADER_ALLOW_SAME_SYMBOL_PARALLEL=false
PAPER_STARTING_EQUITY_USDT=2000
TRADER_PAPER_TAKER_FEE_RATE=0.0008
TRADER_POLL_SECONDS=5
TRADER_PROCESS_EXISTING_SIGNALS=false
TRADER_MAX_SIGNAL_AGE_SECONDS=900
TRADER_PROFIT_TARGET_PCT=20
TRADER_PROTECTION_ARM_PCT=25
TRADER_TRAIL_CALLBACK_PCT=15
TRADER_PROTECTION_UPDATE_STEP_PCT=1
TRADER_PROTECTION_NOTIFY_STEP_PCT=10
TRADER_DISCORD_HEARTBEAT_SECONDS=900
TRADER_ERROR_ALERT_COOLDOWN_SECONDS=300
DISCORD_TRADER_EVENTS_WEBHOOK_URL=<new trader-events Discord webhook>
MEXC_LIVE_ORDER_API_ENABLED=false
```

`TRADER_SLOT_ALLOCATION_PCT` is optional. If omitted, it defaults to `TRADER_MAX_TOTAL_EXPOSURE_PCT / TRADER_MAX_OPEN_POSITIONS`. Set it explicitly only when you intentionally want smaller slots than the aggregate cap permits.

The old `TRADER_CAPITAL_STRATEGY`, `TRADER_POSITION_MATURITY`, `TRADER_ISOLATED_ADVERSE_LIMIT_PCT`, and `TRADER_CROSS_ADVERSE_LIMIT_PCT` environment variables are obsolete for v1.2 strategy execution and should be removed from Render to avoid confusion.

## Discord operations channel

Set the same `DISCORD_TRADER_EVENTS_WEBHOOK_URL` on **both** the trader and scanner Render services. The trader reports opens, target milestones, protection changes, cumulative -100/-200/-300/-400 adverse breaches, closes, API/server errors, recovery, and a periodic portfolio heartbeat. Each notification includes account/capacity/open-position context.

The scanner independently watches the trader's PostgreSQL heartbeat. Set:

```text
TRADER_WATCHDOG_STALE_SECONDS=180
```

on the scanner. If the trader hard-crashes and cannot report its own failure, the scanner alerts after the heartbeat becomes stale and sends a recovery message when the trader returns.

Test the new Discord channel from the trader Render Shell:

```bash
python -m app.trader_notify_test
```

## Status

```bash
python -m app.trader_status
```

## Before switching to live

Keep `TRADING_MODE=paper` while doing the following.

1. Create a MEXC API key with Futures/order-placement permission. Prefer an IP-bound key.
2. Add `MEXC_API_KEY` and `MEXC_API_SECRET` to the trader service only. Never put them on the scanner service.
3. Make sure there are no manual/unmanaged MEXC futures positions. Live startup refuses to run if it finds one.
4. Run the read-only preflight from the trader Render Shell:

```bash
python -m app.trader_preflight
```

5. Only after preflight succeeds, set all three live gates:

```text
TRADING_MODE=live
MEXC_LIVE_ORDER_API_ENABLED=true
LIVE_TRADING_CONFIRM=I_UNDERSTAND_LIVE_TRADING
```

6. Redeploy and confirm the `TRADER ONLINE` Discord event reports `LIVE` mode and the expected strategy before allowing new scanner signals.

The preflight deliberately places no order. Unit/integration tests verify request signing/body construction, current MEXC endpoint selection, multi-slot/risk logic and protection order behavior, but an account-specific live fill cannot be proven without an actual live order.

## Live protection model

When a runner reaches the arm threshold, live mode places a position-level stop at MEXC rather than relying only on the Render process. The bot ratchets that exchange-side stop higher as the runner extends and checks every ~30 seconds that the expected protection order still exists, restoring it if needed. Trigger prices are protection targets, not guaranteed net fills: slippage, fees, gaps, funding and exchange liquidation mechanics still apply.

## Migration

Migration `012_multi_slot_live_trader.sql` is applied automatically by either worker at startup. It removes the old single-open-position database constraint and adds slots, risk tier, protection IDs/timestamps, cumulative adverse-breach timestamps and fee fields.

# Trader deployment — v1.3.4

v1.3.4 does **not** change trader execution. The Render trader service (`mexc-standard-short-trader`; service name kept for deployment continuity) continues to execute the tier-specific strategy selected in v1.3.1.

## Default strategy

- `TRADING_MODE=paper` by default; live remains fail-closed.
- STANDARD + HIGH_RISK only; EXTREME_RISK excluded.
- cross margin, 1x leverage.
- 6 total slots, each ~3.3333% of current equity with the default 20% exposure cap.
- exactly **5 STANDARD capacity + 1 HIGH_RISK capacity**.
- STANDARD: enter immediately on the confirmed signal, ignore +20% as an exit, and close the whole position at **7 days**.
- HIGH_RISK: enter immediately on the confirmed signal, close the whole position at **+20% short return**, or close at **4 days** if +20% has not arrived.
- no conventional tight stop. Existing -100/-200/-300/-400% adverse telemetry remains alert-only.
- no concurrent duplicate symbol by default.
- paper taker fee default: 0.08% per fill.

The Entry Quality and Continuation Risk scores remain research/shadow-only. They do **not** gate live or paper entries in v1.3.4.

## Important deployment behavior for positions already open

Positions opened before v1.3.1 retain their persisted legacy runner/protection strategy. v1.3.1 does not silently rewrite an open trade's exit contract during deployment. New positions opened after deployment receive the tier-specific persisted strategies:

- `fixed_time_standard` + `7d`
- `tp20_or_timeout` + `4d`

If old positions temporarily make the portfolio exceed the new 5/1 tier caps, the trader does not force-close them. It simply refuses new entries in that tier until capacity falls back below the configured cap.

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
TRADER_MAX_STANDARD_POSITIONS=5
TRADER_MAX_HIGH_RISK_POSITIONS=1
TRADER_STANDARD_HOLD_DAYS=7
TRADER_HIGH_RISK_TIMEOUT_DAYS=4
TRADER_ALLOW_SAME_SYMBOL_PARALLEL=false
PAPER_STARTING_EQUITY_USDT=2000
TRADER_PAPER_TAKER_FEE_RATE=0.0008
TRADER_POLL_SECONDS=5
TRADER_PROCESS_EXISTING_SIGNALS=false
TRADER_MAX_SIGNAL_AGE_SECONDS=900
TRADER_PROFIT_TARGET_PCT=20
TRADER_DISCORD_HEARTBEAT_SECONDS=900
TRADER_ERROR_ALERT_COOLDOWN_SECONDS=300
DISCORD_TRADER_EVENTS_WEBHOOK_URL=<trader-events Discord webhook>
MEXC_LIVE_ORDER_API_ENABLED=false
```

`TRADER_SLOT_ALLOCATION_PCT` remains optional. If omitted it defaults to `TRADER_MAX_TOTAL_EXPOSURE_PCT / TRADER_MAX_OPEN_POSITIONS`, so the default is 3.3333% of equity per position.

The legacy protection variables (`TRADER_PROTECTION_ARM_PCT`, `TRADER_TRAIL_CALLBACK_PCT`, etc.) are retained only so already-open pre-v1.3 runner positions can continue to be managed consistently. New v1.3 tier-strategy positions do not use the runner/trailing protection logic.

## Research collection

Keep the scanner setting:

```text
RESEARCH_PATH_HORIZON_HOURS=336
```

Run the research report with:

```bash
python -m app.research_analytics_now
```

High-Risk 1D/2D/3D/4D/5D/7D/10D timeout research now uses the same paired 10-day cohort. This lets the 4-day live timeout continue to be validated fairly against longer alternatives.

### v1.3.4 prospective monitoring

Research-only additions: immediate post-freeze TP5 hit/wait/fail monitoring, rolling-20
EntryGate-v1 acceptance, discovery-vs-post-freeze regime diagnostics, and a compact
post-freeze paired portfolio table once 7-day paths mature. Live execution remains unchanged.

### v1.3.3 prospective strategy lab

Research-only additions: 15m close-marked MTM portfolio risk, frozen EntryGate-v1
(EQ >= 4 and CR <= 6), four-way champion/challenger replay, and a fixed prospective
OOS boundary at 2026-08-21T21:29:00Z. None of these rules gate or resize live trades.

### v1.3.2 TP5 challenger research

The scanner/research service now evaluates a frozen challenger without routing it to the trader:

- 6 generic shadow slots
- 5% of shadow equity notional per slot
- 30% maximum configured shadow exposure
- 1x, immediate entry
- full exit at +5% favorable short return
- one open position per symbol
- 0.08% fee per fill in portfolio replay

The report compares this challenger chronologically against the unchanged live 5-STANDARD/1-HIGH strategy on the same complete-7d signal cohort. It also reports pre-TP5 adverse excursion and +5%-vs-adverse races at -10/-20/-30/-50/-75/-100%. These are research-only outputs.

## Discord operations channel

Set `DISCORD_TRADER_EVENTS_WEBHOOK_URL` on the trader and scanner. New v1.3 positions report their tier-specific exit plan on entry. STANDARD +20% is a milestone notification only. HIGH_RISK +20% produces the position close. Time exits report the persisted close reason.

The scanner independently watches the trader heartbeat. Keep:

```text
TRADER_WATCHDOG_STALE_SECONDS=180
```

Test Discord from the trader Render Shell:

```bash
python -m app.trader_notify_test
```

Check trader status:

```bash
python -m app.trader_status
```

## Before switching to live

Keep `TRADING_MODE=paper` while validating the deployment. Live mode remains deliberately fail-closed.

1. Create a MEXC API key with Futures/order-placement permission; prefer IP binding.
2. Put `MEXC_API_KEY` and `MEXC_API_SECRET` on the trader service only.
3. Ensure there are no unmanaged/manual MEXC futures positions.
4. Run:

```bash
python -m app.trader_preflight
```

5. Only after preflight succeeds, enable all live gates:

```text
TRADING_MODE=live
MEXC_LIVE_ORDER_API_ENABLED=true
LIVE_TRADING_CONFIRM=I_UNDERSTAND_LIVE_TRADING
```

6. Redeploy and verify the startup strategy line shows `STD 5×7d + HIGH 1×TP20/4d` before accepting new signals.

## Migration

Migration `014_tier_exit_strategy.sql` is applied automatically at startup. It expands the persisted trader strategy/maturity constraints for the new tier-specific strategies while keeping all legacy values valid.

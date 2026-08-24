# Trader deployment — v1.3.12

v1.3.12 keeps the **paper trader** on the frozen `TP5_V1` execution strategy from v1.3.6. This release adds research-only behaviour-dependent Hybrid-1 and Hybrid-2 exit replays on the same 6×5% / 30% book; execution semantics are unchanged.
It also contains a research-only path-sync timeout hotfix; no trader execution code or settings are changed.

## Frozen TP5_V1 execution

- `STANDARD` + `HIGH_RISK` only; `EXTREME_RISK` excluded.
- 6 generic slots; there is no 5-Standard/1-High split under TP5.
- exactly 5% of current account equity notional per new position.
- 30% maximum aggregate exposure.
- cross margin, 1x leverage.
- immediate entry on confirmed short.
- one open position per symbol.
- full close at **+5% favorable short return**.
- no conventional tight stop; adverse excursion telemetry remains active.
- paper fee model remains 0.08% per fill for consistency with research.

## Recommended Render environment

```text
MEXC_BASE_URL=https://api.mexc.com
MEXC_WS_URL=wss://contract.mexc.com/edge
TRADING_MODE=paper
TRADER_EXECUTION_STRATEGY=tp5_v1
TRADER_PAPER_RUN_ID=tp5_v1
PAPER_STARTING_EQUITY_USDT=2000
TRADER_ALLOWED_RISK_TIERS=STANDARD,HIGH_RISK
TRADER_MAX_OPEN_POSITIONS=6
TRADER_SLOT_ALLOCATION_PCT=5
TRADER_MAX_TOTAL_EXPOSURE_PCT=30
TRADER_TP5_TARGET_PCT=5
TRADER_ALLOW_SAME_SYMBOL_PARALLEL=false
TRADER_MARGIN_MODE=cross
TRADER_LEVERAGE=1
TRADER_PAPER_TAKER_FEE_RATE=0.0008
TRADER_POLL_SECONDS=5
TRADER_PROCESS_EXISTING_SIGNALS=false
TRADER_MAX_SIGNAL_AGE_SECONDS=900
TRADER_DISCORD_HEARTBEAT_SECONDS=900
TRADER_ERROR_ALERT_COOLDOWN_SECONDS=300
MEXC_LIVE_ORDER_API_ENABLED=false
```

Legacy variables such as `TRADER_MAX_STANDARD_POSITIONS`, `TRADER_MAX_HIGH_RISK_POSITIONS`, `TRADER_STANDARD_HOLD_DAYS`, `TRADER_HIGH_RISK_TIMEOUT_DAYS`, and `TRADER_PROFIT_TARGET_PCT` may remain in Render for rollback compatibility. They are ignored by new `tp5_v1` entries.

## Paper deployment / reset behavior

`TRADER_PAPER_RUN_ID` is the experiment identity. With the default `tp5_v1`:

1. The trader archives/closes any open paper positions belonging to the previous run for historical bookkeeping.
2. Their historical rows are retained; nothing is deleted.
3. The new `tp5_v1` paper run starts at `PAPER_STARTING_EQUITY_USDT` (default **$2,000**).
4. The signal cursor advances to the latest confirmed signal when `TRADER_PROCESS_EXISTING_SIGNALS=false`, so the new run does not retroactively trade old signals.
5. Normal redeploys with the same `TRADER_PAPER_RUN_ID` resume the same positions/equity and **do not reset again**.
6. An archived run ID cannot be reused. For a deliberate future reset, choose a fresh ID such as `tp5_v2`; the configured paper starting equity can also be changed at that time.

Check the current run with:

```bash
python -m app.trader_status
```

## Switching to live later

Paper starting equity is **never** used as live capital. Live mode reads the MEXC Futures USDT account.

The intended live sizing semantics are:

- startup validates positive Futures account equity and exchange-reported **available USDT balance**;
- each TP5 position targets 5% of **current Futures account equity**, preserving equal slot sizing even after other positions reserve margin;
- immediately before an order, the trader also checks exchange-reported available USDT (with an execution buffer) and refuses to open a partial slot if a full 5% slot cannot be funded;
- the 30% aggregate exposure cap and 6-slot cap still apply.

Before live execution:

```bash
python -m app.trader_preflight
```

Then explicitly set:

```text
TRADING_MODE=live
MEXC_LIVE_ORDER_API_ENABLED=true
LIVE_TRADING_CONFIRM=I_UNDERSTAND_LIVE_TRADING
MEXC_API_KEY=<futures-enabled key>
MEXC_API_SECRET=<secret>
```

Prefer an IP-bound API key and ensure no unmanaged/manual Futures positions exist before enabling live mode. Live execution remains fail-closed if preflight/account reconciliation is not safe.

## Migration

Migration `015_tp5_trader_runs.sql` is applied automatically at startup. It adds trader run tracking plus the persisted `tp5_full` exit strategy and `profit_5` maturity contract. Previous migrations remain required and valid.

## Research

v1.3.12 retains the research-only token behaviour classifier while leaving TP5 execution frozen. It uses 90 days of completed pre-signal 4h token/BTC history and periodically backfills the required history for stored research signal symbols.

Keep:

```text
RESEARCH_LOGGING_ENABLED=true
RESEARCH_PATH_HORIZON_HOURS=336
RESEARCH_REGIME_HISTORY_POLL_SECONDS=21600
```

The on-demand board compares TP5-All with: excluding only confident regime followers, episodic-only signals, and same-confirmation-bar episodic priority. Newly listed/insufficient-history coins are not rejected by the conservative no-regime-followers variant.

Run reports with:

```bash
python -m app.research_analytics_now
python -m app.signal_ledger_now
```

The Token Behaviour card also reports capital-time efficiency for all four TP5 shadow books: slot-days, return/slot-day, releases/day, time-based idle capacity across six slots, and average/p95 exposure.

The research report attaches `research-token-regime-YYYY-MM-DD.csv`. No new database migration is required in v1.3.12.

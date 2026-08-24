# Trader deployment — v1.3.17

v1.3.17 keeps the **paper trader** on the frozen `TP5_V1` execution strategy. EXTREME_RISK remains suppressed before signal creation. Public Discord now compares TP5 Frequent, HIGH_RISK TP20 No Timeout, and STANDARD-only 7D Swing on one normalized 168-hour observation window. Trader execution semantics are unchanged.

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

## Research / reporting

v1.3.17 adds account-level run-rate reporting while keeping subscriber strategy selection separate from internal research.

Run:

```bash
python -m app.report_now
python -m app.research_analytics_now
python -m app.signal_ledger_now
```

`python -m app.report_now` publishes the canonical **Strategy Comparison**:
- **TP5 Frequent:** STANDARD + HIGH_RISK, +5% target, no forced timeout.
- **TP20 High Risk:** HIGH_RISK only, +20% target, no forced timeout.
- **7D Swing:** STANDARD only, fixed seven-day exit.

The report now has two deliberately separate layers:
- **Account-Level Return:** chronological portfolio replay using the suggested sizing/capacity, actual exit rule, 0.08% fee per fill, and report-time MTM for open positions. It shows observed account return, a linear **30-Day Equivalent Run-Rate**, equivalent P&L per $10k, entered/open trades, capacity misses, and average/peak exposure. TP20 remains open beyond day 7 until +20%. Funding/slippage are not modeled.
- **168h Signal Evidence:** all three strategies are still valued on the same seven-day signal horizon for path comparison. A target strategy contributes its locked target return when hit before day 7; otherwise it contributes its day-7 mark-to-market. This supporting table shows `Σ signal`, average/median, breach counts while exposed, worst adverse excursion, and capital time.

Suggested exposure shown to subscribers:
- TP5: **5% per trade × 6 / 30% account cap** — portfolio-tested frozen setup.
- TP20: **2% per trade × 5 / 10% account cap** — risk-based suggestion because HIGH_RISK positions can remain unresolved and experience much wider adverse paths.
- STANDARD 7D: **3% per trade × 5 / 15% account cap** — risk-based suggestion for fixed week-long capital occupancy.

The Research Validation Discord board no longer republishes an independent TP20/4D table. It shows evidence health, the frozen TP5 portfolio monitor, and the separate prospective TP5 tracker. Historical TP1/TP2/hybrid/EntryGate/token-behaviour/feature-sweep calculations remain internal.

No new database migration is required in v1.3.17.

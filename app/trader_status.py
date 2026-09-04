from __future__ import annotations

import asyncio
import os

from app.db import Database
from app.trader_db import TraderRepository


async def main() -> None:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    if url.startswith("postgres://"):
        url = "postgresql://" + url.removeprefix("postgres://")
    mode = os.getenv("TRADING_MODE", "paper").strip().lower()
    strategy = os.getenv("TRADER_EXECUTION_STRATEGY", "tp5_sl75_daily_core_persistence_skip_v1").strip().lower()

    db = Database(url)
    await db.connect()
    try:
        repo = TraderRepository(db)
        runtime = await repo.runtime()
        all_positions = await repo.active_positions()
        positions = [p for p in all_positions if p.mode == mode]
        run_id = (
            str(runtime.get("active_run_id") or "legacy_pre_v136")
            if mode == "paper"
            else f"live_{strategy}"
        )
        stats = await repo.portfolio_stats(run_id)

        print(f"Mode: {mode}")
        print(f"Strategy: {strategy}")
        print(f"Active run: {run_id}")
        if mode == "paper":
            unrealized = sum(p.notional_usdt * p.current_return_pct / 100.0 for p in positions)
            paper_cash = float(runtime["paper_equity_usdt"])
            print(f"Paper realized cash: ${paper_cash:,.2f}")
            print(f"Paper MTM equity: ${paper_cash + unrealized:,.2f}")
        print(f"Signal cursor: {int(runtime['last_signal_id'])}")
        print(f"Open {mode} positions: {len(positions)}")
        print(
            f"Current-run closed: {int(stats.get('closed_count') or 0)} | "
            f"Wins: {int(stats.get('win_count') or 0)} | "
            f"Liquidations: {int(stats.get('liquidation_count') or 0)} | "
            f"Fees: ${float(stats.get('fees') or 0):,.4f}"
        )
        for p in positions:
            print(
                f"  slot {p.slot_no}: {p.symbol} {p.risk_tier} {p.mode} | run {p.run_id} | "
                f"entry {p.entry_price:.10g} | current {p.current_price:.10g} | "
                f"return {p.current_return_pct:+.2f}% | peak {p.peak_profit_pct:+.2f}% | "
                f"adverse {p.max_adverse_pct:.2f}% | exit {p.exit_strategy}/{p.position_maturity}"
            )
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

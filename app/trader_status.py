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
    db = Database(url)
    await db.connect()
    try:
        repo = TraderRepository(db)
        runtime = await repo.runtime()
        positions = await repo.active_positions()
        stats = await repo.portfolio_stats()
        unrealized = sum(p.notional_usdt * p.current_return_pct / 100.0 for p in positions if p.mode == "paper")
        paper_cash = float(runtime["paper_equity_usdt"])
        print(f"Paper realized cash: ${paper_cash:,.2f}")
        print(f"Paper MTM equity: ${paper_cash + unrealized:,.2f}")
        print(f"Signal cursor: {int(runtime['last_signal_id'])}")
        print(f"Open positions: {len(positions)}")
        print(
            f"Closed: {int(stats.get('closed_count') or 0)} | Wins: {int(stats.get('win_count') or 0)} | "
            f"Liquidations: {int(stats.get('liquidation_count') or 0)} | Fees: ${float(stats.get('fees') or 0):,.4f}"
        )
        for p in positions:
            print(
                f"  slot {p.slot_no}: {p.symbol} {p.risk_tier} {p.mode} | entry {p.entry_price:.10g} | "
                f"current {p.current_price:.10g} | return {p.current_return_pct:+.2f}% | "
                f"peak {p.peak_profit_pct:+.2f}% | adverse {p.max_adverse_pct:.2f}% | "
                f"exit {p.exit_strategy}/{p.position_maturity} | "
                f"floor {p.profit_floor_pct if p.profit_floor_pct is not None else 'n/a'}"
            )
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

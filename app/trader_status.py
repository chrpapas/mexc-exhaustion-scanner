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
        position = await repo.active_position()
        print(f"Paper equity: ${float(runtime['paper_equity_usdt']):,.2f}")
        print(f"Signal cursor: {int(runtime['last_signal_id'])}")
        if position is None:
            print("Open position: none")
        else:
            print(f"Open position: #{position.id} {position.symbol} ({position.mode}/{position.capital_strategy})")
            print(f"Maturity: {position.position_maturity}")
            print(f"Entry: {position.entry_price:.10g}")
            print(f"Current: {position.current_price:.10g}")
            print(f"Return: {position.current_return_pct:+.2f}%")
            print(f"Peak profit: {position.peak_profit_pct:+.2f}%")
            print(f"Max adverse: {position.max_adverse_pct:.2f}%")
            print(f"Liquidation proxy: +{position.liquidation_proxy_pct:.2f}% adverse")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

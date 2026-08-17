from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from app.config import Settings
from app.db import Database
from app.mexc import MexcClient
from app.notifier import DiscordNotifier
from app.performance import short_return
from app.signal_ledger import build_signal_ledger, signal_ledger_csv
from app.signal_ledger_table import render_signal_ledger_tables

LOGGER = logging.getLogger(__name__)


async def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    db = Database(settings.database_url)
    notifier = DiscordNotifier(
        settings.discord_webhook_url,
        settings.discord_signal_levels,
        performance_webhook_url=settings.discord_performance_webhook_url,
    )
    mexc = MexcClient(
        settings.mexc_base_url,
        spot_base_url=settings.mexc_spot_base_url,
        request_rate_per_second=settings.request_rate_per_second,
        request_concurrency=settings.request_concurrency,
    )

    try:
        await db.connect()
        await db.migrate()
        rows = await db.performance_rows()

        # Current MTM is only used to label still-unresolved signals as currently
        # profitable/negative. Entry and fixed-horizon prices remain persisted
        # shadow analytics and are not replaced by live ticker data.
        try:
            tickers = await mexc.get_tickers()
            current_prices = {ticker.symbol: ticker.last_price for ticker in tickers}
            for row in rows:
                price = current_prices.get(str(row["symbol"]))
                if price is None:
                    continue
                row["current_return_pct"] = short_return(float(row["entry_price"]), price)
        except Exception:
            LOGGER.exception("Live MEXC refresh failed; ledger will use persisted current returns")

        now = datetime.now(UTC)
        ledger = build_signal_ledger(rows, generated_at=now)
        csv_bytes = signal_ledger_csv(ledger)
        table_images = render_signal_ledger_tables(
            ledger, timezone_name=settings.performance_report_timezone
        )
        sent = await notifier.send_signal_ledger(
            ledger,
            csv_bytes=csv_bytes,
            table_images=table_images,
            as_of=now,
            timezone_name=settings.performance_report_timezone,
        )
        if not sent:
            raise RuntimeError(
                "Signal outcome ledger was not sent. Check DISCORD_PERFORMANCE_WEBHOOK_URL "
                "(or DISCORD_WEBHOOK_URL fallback) in Render."
            )

        print(
            "On-demand signal outcome ledger sent to Discord: "
            f"signals={ledger.total} standard={len(ledger.by_risk('standard'))} "
            f"high={len(ledger.by_risk('high_risk'))}"
        )
    finally:
        await mexc.close()
        await notifier.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

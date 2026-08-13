from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from app.config import Settings
from app.db import Database
from app.mexc import MexcClient
from app.notifier import DiscordNotifier
from app.performance import build_performance_summary, short_return

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

        # Refresh open mark-to-market from the live MEXC ticker before building
        # the report. Fixed 1h/4h/12h/24h/48h/72h/7d metrics still come from the persisted
        # shadow-trade tracker, so this command never interferes with the worker.
        try:
            tickers = await mexc.get_tickers()
            current_prices = {ticker.symbol: ticker.last_price for ticker in tickers}
            for row in rows:
                if row.get("return_168h_pct") is not None:
                    continue
                price = current_prices.get(str(row["symbol"]))
                if price is None:
                    continue
                row["current_return_pct"] = short_return(float(row["entry_price"]), price)
        except Exception:
            LOGGER.exception(
                "Live MEXC mark-to-market refresh failed; using latest persisted tracker values"
            )

        now = datetime.now(UTC)
        report = build_performance_summary(
            rows,
            now_utc=now,
            timezone_name=settings.performance_report_timezone,
        )
        sent = await notifier.send_performance_report(
            report,
            label="ON-DEMAND SHADOW PERFORMANCE",
            as_of=now,
            timezone_name=settings.performance_report_timezone,
        )
        if not sent:
            raise RuntimeError(
                "Performance report was not sent. Check DISCORD_PERFORMANCE_WEBHOOK_URL (or DISCORD_WEBHOOK_URL fallback) in Render."
            )

        # Intentionally do not insert into performance_reports. The scheduled
        # daily report therefore remains independently due at its normal time.
        print(
            "On-demand performance report sent to Discord: "
            f"date={report.report_date} open={report.open_count} "
            f"matured24={report.horizon_24h.matured_total} "
            f"matured48={report.horizon_48h.matured_total} "
            f"matured72={report.horizon_72h.matured_total} "
            f"matured7d={report.horizon_168h.matured_total} "
            f"win_rate_7d={report.horizon_168h.win_rate}"
        )
    finally:
        await mexc.close()
        await notifier.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

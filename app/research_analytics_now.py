from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from app.config import Settings
from app.db import Database
from app.notifier import DiscordNotifier
from app.research_analytics import (
    build_research_analytics,
    research_signal_dataset_csv,
)


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

    try:
        await db.connect()
        await db.migrate()

        # Opportunistically catch up one bounded research batch using only rows
        # already present in PostgreSQL. This preserves the v1.2.6 no-extra-MEXC-call rule.
        await db.sync_research_signal_snapshots()
        try:
            await db.sync_research_signal_paths(
                batch_rows=settings.research_path_batch_rows,
                horizon_hours=settings.research_path_horizon_hours,
                statement_timeout_seconds=settings.research_db_timeout_seconds,
            )
        except Exception:
            # Path catch-up is opportunistic research maintenance. A timeout must not
            # suppress an otherwise valid on-demand report from already persisted data.
            logging.exception(
                "Research path catch-up failed; continuing with currently persisted paths"
            )

        rows = await db.research_analytics_rows()
        try:
            portfolio_path_rows = await db.research_portfolio_path_rows(
                statement_timeout_seconds=settings.research_db_timeout_seconds,
            )
        except Exception:
            logging.exception("Portfolio MTM research query failed; continuing without MTM marks")
            portfolio_path_rows = []
        now = datetime.now(UTC)
        report = build_research_analytics(
            rows,
            generated_at=now,
            portfolio_path_rows=portfolio_path_rows,
        )
        dataset_csv = research_signal_dataset_csv(rows)

        sent = await notifier.send_research_analytics(
            report,
            dataset_csv=dataset_csv,
            as_of=now,
            timezone_name=settings.performance_report_timezone,
        )
        if not sent:
            raise RuntimeError(
                "Research analytics were not sent. Check DISCORD_PERFORMANCE_WEBHOOK_URL "
                "(or DISCORD_WEBHOOK_URL fallback) in Render."
            )

        b = report.baseline
        print(
            "On-demand research analytics sent to Discord: "
            f"signals={b.total_signals} matured7d={b.matured_7d} "
            f"complete_paths7d={b.complete_paths_7d} complete_paths14d={b.complete_paths_14d} "
            f"target20_7d={b.target_20_rate_7d} positive7d={b.positive_7d_rate}"
        )
    finally:
        await notifier.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

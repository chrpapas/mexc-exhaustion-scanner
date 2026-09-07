from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from app.config import Settings
from app.db import Database
from app.notifier import DiscordNotifier
from app.research_analytics import (
    build_current_strategy_research as build_research_analytics,
    research_current_strategy_csv,
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
            await db.backfill_research_daily_regime_features(
                statement_timeout_seconds=settings.research_db_timeout_seconds,
            )
        except Exception:
            logging.exception(
                "Daily regime DB backfill failed; continuing with currently persisted 1D features"
            )
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
        # Keep the raw signal dataset for future analysis, but stop generating and
        # uploading legacy strategy-sweep/feature/regime bundles on every Discord run.
        dataset_csv = research_signal_dataset_csv(rows, generated_at=now)
        strategy_csv = research_current_strategy_csv(report) if hasattr(report, "strategy") else None

        sent = await notifier.send_research_analytics(
            report,
            dataset_csv=dataset_csv,
            strategy_csv=strategy_csv,
            as_of=now,
            timezone_name=settings.performance_report_timezone,
        )
        if not sent:
            raise RuntimeError(
                "Research analytics were not sent. Check DISCORD_PERFORMANCE_WEBHOOK_URL "
                "(or DISCORD_WEBHOOK_URL fallback) in Render."
            )

        if hasattr(report, "total_signals"):
            print(
                "On-demand current-strategy research sent to Discord: "
                f"signals={report.total_signals} admitted={report.admitted_signals} "
                f"daily_core_filtered={report.daily_core_flagged + report.daily_core_missing} "
                f"persistence_filtered={report.persistence_flagged + report.persistence_missing} "
                f"forward_signals={report.prospective_total_signals} "
                f"forward_admitted={report.prospective_admitted_signals}"
            )
        else:
            b = report.baseline
            print(
                "On-demand current-strategy research sent to Discord: "
                f"signals={b.total_signals}"
            )
    finally:
        await notifier.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

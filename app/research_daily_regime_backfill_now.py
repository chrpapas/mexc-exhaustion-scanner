from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from app.config import Settings
from app.worker import ScannerWorker


async def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    worker = ScannerWorker(settings)
    try:
        await worker.db.connect()
        await worker.db.migrate()
        requirements = await worker.db.research_regime_history_requirements(lookback_days=45)
        semaphore = asyncio.Semaphore(settings.request_concurrency)

        async def sync(item: dict[str, object]) -> None:
            symbol = str(item.get("symbol") or "")
            required_start = item.get("required_start")
            if not symbol or not isinstance(required_start, datetime):
                return
            async with semaphore:
                await worker._sync_interval_from(
                    symbol, "Day1", desired_start=required_start, overlap_hours=48
                )

        results = await asyncio.gather(*(sync(item) for item in requirements), return_exceptions=True)
        failures = [result for result in results if isinstance(result, Exception)]
        for error in failures:
            logging.warning("Daily regime history sync failed: %s", error)
        print(
            f"Daily regime history sync: symbols={len(requirements)} failures={len(failures)}"
        )

        batch_size = 64
        total = 0
        for round_no in range(1, 51):
            updated = await worker.db.backfill_research_daily_regime_features(
                batch_size=batch_size,
                retry_missing=False,
                statement_timeout_seconds=settings.research_db_timeout_seconds,
            )
            total += updated
            print(f"Daily regime batch {round_no}: updated={updated} cumulative={total}")
            if updated < batch_size:
                break

        retry_cutoff = datetime.now(UTC)
        retry_total = 0
        for round_no in range(1, 51):
            updated = await worker.db.backfill_research_daily_regime_features(
                batch_size=batch_size,
                retry_missing=True,
                retry_before=retry_cutoff,
                statement_timeout_seconds=settings.research_db_timeout_seconds,
            )
            retry_total += updated
            print(
                f"Daily regime retry batch {round_no}: updated={updated} "
                f"retry_cumulative={retry_total}"
            )
            if updated < batch_size:
                break

        rows = await worker.db.research_analytics_rows()
        computable = 0
        bullish = 0
        missing = 0
        for row in rows:
            snapshot = row.get("feature_snapshot") or {}
            if isinstance(snapshot, str):
                import json
                try:
                    snapshot = json.loads(snapshot)
                except json.JSONDecodeError:
                    snapshot = {}
            if not isinstance(snapshot, dict):
                snapshot = {}
            if snapshot.get("daily_regime_v1_computable") is True:
                computable += 1
                if snapshot.get("daily_regime_v1_bullish") is True:
                    bullish += 1
            else:
                missing += 1
        print(
            "Daily regime backfill complete: "
            f"initial_updated={total} retries_updated={retry_total} "
            f"computable={computable} missing={missing} bullish={bullish}"
        )
    finally:
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())

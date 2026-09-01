from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from app.config import Settings
from app.db import Database


async def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    db = Database(settings.database_url)
    try:
        await db.connect()
        await db.migrate()
        snapshots = await db.sync_research_signal_snapshots()

        batch_size = 64
        total = 0
        rounds = 0
        # First drain rows never attempted under the current HTF rule version.
        while rounds < 50:
            updated = await db.backfill_research_htf_features(
                batch_size=batch_size,
                retry_missing=False,
                statement_timeout_seconds=settings.research_db_timeout_seconds,
            )
            rounds += 1
            total += updated
            print(f"HTF backfill batch {rounds}: updated={updated} cumulative={total}")
            if updated < batch_size:
                break

        # Retry previously attempted rows missing candle-derivable fields once
        # against one stable frontier. This cannot loop forever on rows whose
        # only missing field is the unreconstructable cross-sectional percentile.
        retry_cutoff = datetime.now(UTC)
        retry_total = 0
        for retry_round in range(1, 51):
            updated = await db.backfill_research_htf_features(
                batch_size=batch_size,
                retry_missing=True,
                retry_before=retry_cutoff,
                statement_timeout_seconds=settings.research_db_timeout_seconds,
            )
            retry_total += updated
            print(
                f"HTF retry batch {retry_round}: updated={updated} "
                f"retry_cumulative={retry_total}"
            )
            if updated < batch_size:
                break

        metadata = await db.sync_research_htf_metadata()
        rows = await db.research_analytics_rows()
        computable = 0
        flagged = 0
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
            if snapshot.get("htf_v1_computable") is True:
                computable += 1
                if snapshot.get("htf_v1_flagged") is True:
                    flagged += 1
            else:
                missing += 1

        print(
            "HTF backfill complete: "
            f"snapshots_synced={snapshots} initial_updated={total} retries_updated={retry_total} "
            f"metadata_updated={metadata} computable={computable} missing={missing} flagged={flagged}"
        )
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

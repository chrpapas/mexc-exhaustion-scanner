from __future__ import annotations

import asyncio

from app.config import Settings
from app.db import Database


async def main() -> None:
    settings = Settings.from_env()
    db = Database(settings.database_url)
    await db.connect()
    try:
        await db.migrate()
        counts = await db.research_signal_counts()
        print("Research logging status")
        print(f"feature snapshots: {counts['feature_rows']}")
        print(f"15m path rows: {counts['path_rows']}")
        print(f"last path candle: {counts['last_path_at'] or 'n/a'}")
        print(f"enabled: {settings.research_logging_enabled}")
        print(f"poll seconds: {settings.research_path_poll_seconds}")
        print(f"batch rows: {settings.research_path_batch_rows}")
        print(f"horizon hours: {settings.research_path_horizon_hours}")
        print(f"DB timeout seconds: {settings.research_db_timeout_seconds}")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

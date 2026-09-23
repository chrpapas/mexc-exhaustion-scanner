#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path


def parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        out = value
    else:
        out = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if out.tzinfo is None:
        out = out.replace(tzinfo=UTC)
    return out.astimezone(UTC)


async def export_overlay(start: datetime, end: datetime, output: Path) -> int:
    try:
        import asyncpg
    except ImportError as exc:
        raise RuntimeError("asyncpg is required; run this inside the Render scanner service environment") from exc

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        from app.config import Settings
        database_url = Settings.from_env().database_url

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    sq = sqlite3.connect(output)
    sq.execute("PRAGMA journal_mode=WAL")
    sq.execute("PRAGMA synchronous=NORMAL")
    sq.execute(
        """
        CREATE TABLE ticker_snapshots (
            symbol TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            last_price REAL,
            bid1 REAL,
            ask1 REAL,
            amount24 REAL,
            volume24 REAL,
            hold_vol REAL,
            low24 REAL,
            high24 REAL,
            rise_fall_rate REAL,
            index_price REAL,
            fair_price REAL,
            funding_rate REAL
        )
        """
    )
    insert_sql = """
        INSERT INTO ticker_snapshots (
            symbol, observed_at, last_price, bid1, ask1, amount24, volume24,
            hold_vol, low24, high24, rise_fall_rate, index_price, fair_price,
            funding_rate
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """

    conn = await asyncpg.connect(database_url, command_timeout=120)
    count = 0
    batch = []
    try:
        # Include one signal interval before the requested start so replay can
        # causally carry a fresh ticker into the first validation bucket.
        query_start = start - timedelta(minutes=5)
        async with conn.transaction():
            cursor = conn.cursor(
                """
                SELECT symbol, observed_at, last_price, bid1, ask1, amount24,
                       volume24, hold_vol, low24, high24, rise_fall_rate,
                       index_price, fair_price, funding_rate
                FROM ticker_snapshots
                WHERE observed_at >= $1 AND observed_at <= $2
                ORDER BY observed_at, symbol
                """,
                query_start,
                end,
                prefetch=2000,
            )
            async for row in cursor:
                observed = row["observed_at"]
                if isinstance(observed, datetime):
                    if observed.tzinfo is None:
                        observed = observed.replace(tzinfo=UTC)
                    observed_text = observed.astimezone(UTC).isoformat()
                else:
                    observed_text = str(observed)
                batch.append(
                    (
                        row["symbol"], observed_text, row["last_price"], row["bid1"],
                        row["ask1"], row["amount24"], row["volume24"], row["hold_vol"],
                        row["low24"], row["high24"], row["rise_fall_rate"],
                        row["index_price"], row["fair_price"], row["funding_rate"],
                    )
                )
                if len(batch) >= 5000:
                    sq.executemany(insert_sql, batch)
                    sq.commit()
                    count += len(batch)
                    batch.clear()
        if batch:
            sq.executemany(insert_sql, batch)
            sq.commit()
            count += len(batch)
        sq.execute("CREATE INDEX idx_ticker_observed_symbol ON ticker_snapshots(observed_at, symbol)")
        sq.execute("CREATE INDEX idx_ticker_symbol_observed ON ticker_snapshots(symbol, observed_at)")
        sq.commit()
    finally:
        await conn.close()
        sq.close()
    return count


def main() -> int:
    p = argparse.ArgumentParser(description="Read-only production ticker snapshot export to replay-compatible SQLite")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    start, end = parse_dt(args.start), parse_dt(args.end)
    if end <= start:
        raise SystemExit("--end must be after --start")
    output = Path(args.output).expanduser().resolve()
    count = asyncio.run(export_overlay(start, end, output))
    print(f"ticker overlay rows={count:,} path={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

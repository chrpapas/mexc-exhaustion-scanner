#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import asyncpg

TARGETS = [
    # symbol, candle open_time, production signal-time high, production signal-time close
    ("APT_USDT",    "2026-09-09T07:30:00+00:00", 0.6537,    0.6527),
    ("BTW_USDT",    "2026-09-11T08:15:00+00:00", 0.50305,   0.50154),
    ("LAB_USDT",    "2026-09-12T08:15:00+00:00", 0.07319,   0.0721),
    ("LIGHT_USDT",  "2026-09-12T21:45:00+00:00", 0.1627,    0.1625),
    ("BR_USDT",     "2026-09-14T00:15:00+00:00", 0.31787,   0.3144),
    ("IOTX_USDT",   "2026-09-14T08:00:00+00:00", 0.003432,  0.003416),
    ("VTHO_USDT",   "2026-09-15T15:15:00+00:00", 0.0007605, 0.0007594),
    ("PUFFER_USDT", "2026-09-16T05:15:00+00:00", 0.02486,   0.02453),
]

def dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)

async def main() -> None:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is not set")

    conn = await asyncpg.connect(database_url)
    try:
        print(
            "symbol,candle_time,"
            "signal_time_high,db_now_high,delta_high,"
            "signal_time_close,db_now_close,delta_close,"
            "db_open,db_low,db_volume,db_amount"
        )

        final_like = 0
        signal_like = 0

        for symbol, ts, sig_high, sig_close in TARGETS:
            row = await conn.fetchrow(
                """
                SELECT symbol, interval, open_time,
                       open, high, low, close, volume, amount
                FROM candles
                WHERE symbol = $1
                  AND interval = 'Min15'
                  AND open_time = $2
                """,
                symbol,
                dt(ts),
            )

            if row is None:
                print(f"{symbol},{ts},MISSING")
                continue

            db_high = float(row["high"])
            db_close = float(row["close"])
            dh = db_high - sig_high
            dc = db_close - sig_close

            # A non-zero difference proves the row was changed after the
            # signal-time values were captured in the production signal.
            if abs(dh) < 1e-15 and abs(dc) < 1e-15:
                signal_like += 1
            else:
                final_like += 1

            print(
                f"{symbol},{row['open_time'].isoformat()},"
                f"{sig_high:.15g},{db_high:.15g},{dh:+.15g},"
                f"{sig_close:.15g},{db_close:.15g},{dc:+.15g},"
                f"{float(row['open']):.15g},{float(row['low']):.15g},"
                f"{float(row['volume']):.15g},{float(row['amount']):.15g}"
            )

        print()
        print("Rows identical to signal-time high+close:", signal_like)
        print("Rows changed since signal-time snapshot:", final_like)
        print()
        if final_like:
            print(
                "INTERPRETATION: at least some candle rows currently stored in PostgreSQL "
                "differ from the OHLC values that production actually used at signal time. "
                "Because candles are upserted, later candle refreshes can overwrite the "
                "earlier point-in-time snapshot."
            )
        else:
            print(
                "INTERPRETATION: all eight current PostgreSQL rows still match the "
                "signal-time high+close. In that case we can export these production "
                "candles directly for validation."
            )
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(main())

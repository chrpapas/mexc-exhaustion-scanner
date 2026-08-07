from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg

from app.models import Candle, RunSignal, Ticker


class Database:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database is not connected")
        return self._pool

    async def connect(self) -> None:
        last_error: Exception | None = None
        for attempt in range(1, 11):
            try:
                self._pool = await asyncpg.create_pool(
                    self._database_url,
                    min_size=1,
                    max_size=8,
                    command_timeout=60,
                )
                return
            except Exception as exc:
                last_error = exc
                if attempt < 10:
                    await asyncio.sleep(min(attempt * 2, 15))
        raise RuntimeError(f"Could not connect to PostgreSQL: {last_error}")

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def migrate(self) -> None:
        migration_dir = Path(__file__).resolve().parents[1] / "migrations"
        for path in sorted(migration_dir.glob("*.sql")):
            await self.pool.execute(path.read_text(encoding="utf-8"))

    async def upsert_contracts(self, rows: list[dict[str, Any]]) -> None:
        now = datetime.now(UTC)
        values = []
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            values.append(
                (
                    symbol,
                    str(row.get("baseCoin") or ""),
                    str(row.get("quoteCoin") or ""),
                    str(row.get("settleCoin") or ""),
                    int(row.get("state") or 0),
                    bool(row.get("isHidden", False)),
                    bool(row.get("apiAllowed", False)),
                    json.dumps(row, separators=(",", ":"), default=str),
                    now,
                    now,
                )
            )
        if not values:
            return
        query = """
            INSERT INTO contracts (
                symbol, base_coin, quote_coin, settle_coin, state, is_hidden,
                api_allowed, metadata, first_seen_at, last_seen_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10)
            ON CONFLICT (symbol) DO UPDATE SET
                base_coin = EXCLUDED.base_coin,
                quote_coin = EXCLUDED.quote_coin,
                settle_coin = EXCLUDED.settle_coin,
                state = EXCLUDED.state,
                is_hidden = EXCLUDED.is_hidden,
                api_allowed = EXCLUDED.api_allowed,
                metadata = EXCLUDED.metadata,
                last_seen_at = EXCLUDED.last_seen_at
        """
        await self.pool.executemany(query, values)

    async def active_usdt_contracts(self) -> set[str]:
        rows = await self.pool.fetch(
            """
            SELECT symbol
            FROM contracts
            WHERE quote_coin = 'USDT'
              AND settle_coin = 'USDT'
              AND state = 0
              AND is_hidden = false
            """
        )
        return {str(row["symbol"]) for row in rows}

    async def insert_tickers(self, tickers: list[Ticker]) -> None:
        if not tickers:
            return
        query = """
            INSERT INTO ticker_snapshots (
                symbol, observed_at, last_price, bid1, ask1, spread_pct,
                amount24, volume24, hold_vol, low24, high24, rise_fall_rate,
                index_price, fair_price, funding_rate
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
            ON CONFLICT (symbol, observed_at) DO UPDATE SET
                last_price = EXCLUDED.last_price,
                bid1 = EXCLUDED.bid1,
                ask1 = EXCLUDED.ask1,
                spread_pct = EXCLUDED.spread_pct,
                amount24 = EXCLUDED.amount24,
                volume24 = EXCLUDED.volume24,
                hold_vol = EXCLUDED.hold_vol,
                low24 = EXCLUDED.low24,
                high24 = EXCLUDED.high24,
                rise_fall_rate = EXCLUDED.rise_fall_rate,
                index_price = EXCLUDED.index_price,
                fair_price = EXCLUDED.fair_price,
                funding_rate = EXCLUDED.funding_rate
        """
        values = [
            (
                item.symbol,
                item.observed_at,
                item.last_price,
                item.bid1,
                item.ask1,
                item.spread_pct,
                item.amount24,
                item.volume24,
                item.hold_vol,
                item.low24,
                item.high24,
                item.rise_fall_rate,
                item.index_price,
                item.fair_price,
                item.funding_rate,
            )
            for item in tickers
        ]
        await self.pool.executemany(query, values)

    async def latest_candle_time(self, symbol: str, interval: str) -> datetime | None:
        return await self.pool.fetchval(
            "SELECT max(open_time) FROM candles WHERE symbol=$1 AND interval=$2",
            symbol,
            interval,
        )

    async def upsert_candles(self, candles: list[Candle]) -> None:
        if not candles:
            return
        query = """
            INSERT INTO candles (
                symbol, interval, open_time, open, high, low, close, volume, amount
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (symbol, interval, open_time) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume,
                amount = EXCLUDED.amount
        """
        await self.pool.executemany(
            query,
            [
                (
                    item.symbol,
                    item.interval,
                    item.open_time,
                    item.open,
                    item.high,
                    item.low,
                    item.close,
                    item.volume,
                    item.amount,
                )
                for item in candles
            ],
        )

    async def fetch_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        rows = await self.pool.fetch(
            """
            SELECT symbol, interval, open_time, open, high, low, close, volume, amount
            FROM candles
            WHERE symbol=$1 AND interval=$2
            ORDER BY open_time DESC
            LIMIT $3
            """,
            symbol,
            interval,
            limit,
        )
        return [
            Candle(
                symbol=str(row["symbol"]),
                interval=str(row["interval"]),
                open_time=row["open_time"],
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                amount=float(row["amount"]),
            )
            for row in reversed(rows)
        ]

    async def upsert_funding_history(self, symbol: str, rows: list[dict[str, Any]]) -> None:
        values = []
        for row in rows:
            try:
                settle_time = datetime.fromtimestamp(int(row["settleTime"]) / 1000, UTC)
                funding_rate = float(row["fundingRate"])
            except (KeyError, TypeError, ValueError, OSError):
                continue
            values.append((symbol, settle_time, funding_rate))
        if values:
            await self.pool.executemany(
                """
                INSERT INTO funding_rates (symbol, settle_time, funding_rate)
                VALUES ($1,$2,$3)
                ON CONFLICT (symbol, settle_time) DO UPDATE SET
                    funding_rate = EXCLUDED.funding_rate
                """,
                values,
            )

    async def recently_alerted(self, symbol: str, cooldown_minutes: int, level: str = "candidate") -> bool:
        cutoff = datetime.now(UTC) - timedelta(minutes=cooldown_minutes)
        return bool(
            await self.pool.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM run_signals
                    WHERE symbol=$1 AND level=$3 AND signaled_at >= $2
                )
                """,
                symbol,
                cutoff,
                level,
            )
        )

    async def insert_signal(self, signal: RunSignal) -> bool:
        result = await self.pool.execute(
            """
            INSERT INTO run_signals (symbol, signaled_at, level, score, features, reasons)
            VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb)
            ON CONFLICT (symbol, signaled_at, level) DO NOTHING
            """,
            signal.symbol,
            signal.signaled_at,
            signal.level,
            signal.score,
            json.dumps(signal.features, separators=(",", ":"), default=str),
            json.dumps(signal.reasons, separators=(",", ":")),
        )
        return result == "INSERT 0 1"

    async def heartbeat(self, worker_name: str, status: dict[str, Any]) -> None:
        await self.pool.execute(
            """
            INSERT INTO worker_heartbeat (worker_name, last_seen_at, status)
            VALUES ($1, now(), $2::jsonb)
            ON CONFLICT (worker_name) DO UPDATE SET
                last_seen_at = EXCLUDED.last_seen_at,
                status = EXCLUDED.status
            """,
            worker_name,
            json.dumps(status, separators=(",", ":"), default=str),
        )

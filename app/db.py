from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg

from app.models import Candle, PumpEpisode, RunSignal, Ticker


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
        await self.pool.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )

        for path in sorted(migration_dir.glob("*.sql")):
            filename = path.name
            already_applied = await self.pool.fetchval(
                "SELECT 1 FROM schema_migrations WHERE filename=$1",
                filename,
            )
            if already_applied:
                continue

            sql = path.read_text(encoding="utf-8")
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations(filename) VALUES ($1)",
                        filename,
                    )

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

    async def recently_alerted(self, symbol: str, cooldown_minutes: int, level: str) -> bool:
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
            INSERT INTO run_signals (
                symbol, signaled_at, level, score, features, reasons, episode_id
            )
            VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7)
            ON CONFLICT (symbol, signaled_at, level) DO NOTHING
            """,
            signal.symbol,
            signal.signaled_at,
            signal.level,
            signal.score,
            json.dumps(signal.features, separators=(",", ":"), default=str),
            json.dumps(signal.reasons, separators=(",", ":")),
            signal.episode_id,
        )
        return result == "INSERT 0 1"

    async def active_episode_symbols(self) -> set[str]:
        rows = await self.pool.fetch(
            """
            SELECT DISTINCT symbol
            FROM pump_episodes
            WHERE closed_at IS NULL
            """
        )
        return {str(row["symbol"]) for row in rows}

    async def get_active_episode(self, symbol: str) -> PumpEpisode | None:
        row = await self.pool.fetchrow(
            """
            SELECT id, symbol, started_at, updated_at, state, peak_price, peak_at,
                   broken_level, breakdown_at, breakdown_atr_15m, retest_at,
                   confirmed_short_at, closed_at, last_run_score,
                   last_exhaustion_score, metadata
            FROM pump_episodes
            WHERE symbol=$1 AND closed_at IS NULL
            ORDER BY started_at DESC
            LIMIT 1
            """,
            symbol,
        )
        return self._episode_from_row(row) if row else None

    async def create_episode(
        self,
        *,
        symbol: str,
        started_at: datetime,
        state: str,
        peak_price: float,
        peak_at: datetime,
        run_score: int,
        exhaustion_score: int,
        metadata: dict[str, Any] | None = None,
    ) -> PumpEpisode:
        row = await self.pool.fetchrow(
            """
            INSERT INTO pump_episodes (
                symbol, started_at, updated_at, state, peak_price, peak_at,
                last_run_score, last_exhaustion_score, metadata
            ) VALUES ($1,$2,$2,$3,$4,$5,$6,$7,$8::jsonb)
            RETURNING id, symbol, started_at, updated_at, state, peak_price, peak_at,
                      broken_level, breakdown_at, breakdown_atr_15m, retest_at,
                      confirmed_short_at, closed_at, last_run_score,
                      last_exhaustion_score, metadata
            """,
            symbol,
            started_at,
            state,
            peak_price,
            peak_at,
            run_score,
            exhaustion_score,
            json.dumps(metadata or {}, separators=(",", ":"), default=str),
        )
        assert row is not None
        return self._episode_from_row(row)

    async def update_episode(
        self,
        episode_id: int,
        *,
        state: str | None = None,
        peak_price: float | None = None,
        peak_at: datetime | None = None,
        broken_level: float | None = None,
        breakdown_at: datetime | None = None,
        breakdown_atr_15m: float | None = None,
        retest_at: datetime | None = None,
        confirmed_short_at: datetime | None = None,
        run_score: int | None = None,
        exhaustion_score: int | None = None,
        metadata: dict[str, Any] | None = None,
        clear_breakdown: bool = False,
    ) -> PumpEpisode:
        row = await self.pool.fetchrow(
            """
            UPDATE pump_episodes
            SET updated_at = now(),
                state = COALESCE($2, state),
                peak_price = COALESCE($3, peak_price),
                peak_at = COALESCE($4, peak_at),
                broken_level = CASE WHEN $13 THEN NULL ELSE COALESCE($5, broken_level) END,
                breakdown_at = CASE WHEN $13 THEN NULL ELSE COALESCE($6, breakdown_at) END,
                breakdown_atr_15m = CASE WHEN $13 THEN NULL ELSE COALESCE($7, breakdown_atr_15m) END,
                retest_at = CASE WHEN $13 THEN NULL ELSE COALESCE($8, retest_at) END,
                confirmed_short_at = COALESCE($9, confirmed_short_at),
                last_run_score = COALESCE($10, last_run_score),
                last_exhaustion_score = COALESCE($11, last_exhaustion_score),
                metadata = CASE
                    WHEN $12::jsonb IS NULL THEN metadata
                    ELSE metadata || $12::jsonb
                END
            WHERE id=$1
            RETURNING id, symbol, started_at, updated_at, state, peak_price, peak_at,
                      broken_level, breakdown_at, breakdown_atr_15m, retest_at,
                      confirmed_short_at, closed_at, last_run_score,
                      last_exhaustion_score, metadata
            """,
            episode_id,
            state,
            peak_price,
            peak_at,
            broken_level,
            breakdown_at,
            breakdown_atr_15m,
            retest_at,
            confirmed_short_at,
            run_score,
            exhaustion_score,
            json.dumps(metadata, separators=(",", ":"), default=str) if metadata is not None else None,
            clear_breakdown,
        )
        if row is None:
            raise RuntimeError(f"Episode {episode_id} not found")
        return self._episode_from_row(row)

    async def close_episode(
        self,
        episode_id: int,
        *,
        closed_at: datetime,
        reason: str,
    ) -> None:
        await self.pool.execute(
            """
            UPDATE pump_episodes
            SET closed_at=$2,
                updated_at=now(),
                metadata = metadata || jsonb_build_object('closed_reason', $3)
            WHERE id=$1 AND closed_at IS NULL
            """,
            episode_id,
            closed_at,
            reason,
        )

    @staticmethod
    def _episode_from_row(row: asyncpg.Record) -> PumpEpisode:
        raw_metadata = row["metadata"]
        if isinstance(raw_metadata, str):
            try:
                metadata = json.loads(raw_metadata)
            except json.JSONDecodeError:
                metadata = {}
        elif isinstance(raw_metadata, dict):
            metadata = raw_metadata
        else:
            metadata = {}
        return PumpEpisode(
            id=int(row["id"]),
            symbol=str(row["symbol"]),
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            state=str(row["state"]),
            peak_price=float(row["peak_price"]),
            peak_at=row["peak_at"],
            broken_level=float(row["broken_level"]) if row["broken_level"] is not None else None,
            breakdown_at=row["breakdown_at"],
            breakdown_atr_15m=(
                float(row["breakdown_atr_15m"])
                if row["breakdown_atr_15m"] is not None
                else None
            ),
            retest_at=row["retest_at"],
            confirmed_short_at=row["confirmed_short_at"],
            closed_at=row["closed_at"],
            last_run_score=int(row["last_run_score"]),
            last_exhaustion_score=int(row["last_exhaustion_score"]),
            metadata=metadata,
        )


    async def create_shadow_trade(
        self,
        *,
        episode_id: int,
        symbol: str,
        confirmed_at: datetime,
        entry_price: float,
        risk_tier: str = "standard",
    ) -> bool:
        result = await self.pool.execute(
            """
            INSERT INTO shadow_trades (episode_id, symbol, confirmed_at, entry_price, risk_tier)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (episode_id) DO NOTHING
            """,
            episode_id,
            symbol,
            confirmed_at,
            entry_price,
            risk_tier,
        )
        return result == "INSERT 0 1"

    async def fetch_shadow_trades(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT episode_id, symbol, confirmed_at, entry_price, risk_tier, current_price,
                   current_return_pct, last_observed_at, mfe_pct, mae_pct,
                   return_1h_pct, return_4h_pct, return_12h_pct, return_24h_pct,
                   matured_at
            FROM shadow_trades
            ORDER BY confirmed_at ASC
            """
        )
        return [dict(row) for row in rows]

    async def candle_excursions(
        self,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[float | None, float | None]:
        row = await self.pool.fetchrow(
            """
            SELECT min(low) AS min_low, max(high) AS max_high
            FROM candles
            WHERE symbol=$1
              AND interval='Min15'
              AND open_time >= $2 - interval '15 minutes'
              AND open_time < $3
            """,
            symbol,
            start_at,
            end_at,
        )
        if row is None:
            return None, None
        return (
            float(row["min_low"]) if row["min_low"] is not None else None,
            float(row["max_high"]) if row["max_high"] is not None else None,
        )

    async def candle_close_for_horizon(
        self,
        symbol: str,
        target_at: datetime,
    ) -> tuple[datetime, float] | None:
        row = await self.pool.fetchrow(
            """
            SELECT open_time + interval '15 minutes' AS close_time, close
            FROM candles
            WHERE symbol=$1
              AND interval='Min15'
              AND open_time + interval '15 minutes' >= $2
              AND open_time + interval '15 minutes' <= $2 + interval '30 minutes'
            ORDER BY open_time ASC
            LIMIT 1
            """,
            symbol,
            target_at,
        )
        if row is None:
            return None
        return row["close_time"], float(row["close"])

    async def update_shadow_trade(
        self,
        episode_id: int,
        *,
        current_price: float | None = None,
        current_return_pct: float | None = None,
        observed_at: datetime | None = None,
        mfe_pct: float | None = None,
        mae_pct: float | None = None,
        return_1h_pct: float | None = None,
        return_4h_pct: float | None = None,
        return_12h_pct: float | None = None,
        return_24h_pct: float | None = None,
        matured_at: datetime | None = None,
    ) -> None:
        await self.pool.execute(
            """
            UPDATE shadow_trades
            SET current_price = COALESCE($2, current_price),
                current_return_pct = COALESCE($3, current_return_pct),
                last_observed_at = COALESCE($4, last_observed_at),
                mfe_pct = CASE WHEN $5::double precision IS NULL THEN mfe_pct ELSE GREATEST(mfe_pct, $5) END,
                mae_pct = CASE WHEN $6::double precision IS NULL THEN mae_pct ELSE LEAST(mae_pct, $6) END,
                return_1h_pct = COALESCE(return_1h_pct, $7),
                return_4h_pct = COALESCE(return_4h_pct, $8),
                return_12h_pct = COALESCE(return_12h_pct, $9),
                return_24h_pct = COALESCE(return_24h_pct, $10),
                matured_at = COALESCE(matured_at, $11),
                updated_at = now()
            WHERE episode_id=$1
            """,
            episode_id,
            current_price,
            current_return_pct,
            observed_at,
            mfe_pct,
            mae_pct,
            return_1h_pct,
            return_4h_pct,
            return_12h_pct,
            return_24h_pct,
            matured_at,
        )

    async def last_performance_report_date(self):
        return await self.pool.fetchval("SELECT max(report_date) FROM performance_reports")

    async def performance_rows(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT episode_id, symbol, confirmed_at, entry_price, risk_tier,
                   current_return_pct, mfe_pct, mae_pct,
                   return_1h_pct, return_4h_pct, return_12h_pct, return_24h_pct,
                   matured_at
            FROM shadow_trades
            ORDER BY confirmed_at ASC
            """
        )
        return [dict(row) for row in rows]

    async def record_performance_report(
        self,
        *,
        report_date,
        sent_at: datetime,
        timezone_name: str,
        payload: dict[str, Any],
    ) -> bool:
        result = await self.pool.execute(
            """
            INSERT INTO performance_reports (report_date, sent_at, timezone, payload)
            VALUES ($1,$2,$3,$4::jsonb)
            ON CONFLICT (report_date) DO NOTHING
            """,
            report_date,
            sent_at,
            timezone_name,
            json.dumps(payload, separators=(",", ":"), default=str),
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

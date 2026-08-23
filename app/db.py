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
        async with self.pool.acquire() as conn:
            # The scanner and trader are separate Render workers sharing one DB.
            # Serialize schema migrations so simultaneous deploys cannot race.
            await conn.execute("SELECT pg_advisory_lock(hashtext('mexc_exhaustion_schema_migrations'))")
            try:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        filename text PRIMARY KEY,
                        applied_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )

                for path in sorted(migration_dir.glob("*.sql")):
                    filename = path.name
                    already_applied = await conn.fetchval(
                        "SELECT 1 FROM schema_migrations WHERE filename=$1",
                        filename,
                    )
                    if already_applied:
                        continue

                    sql = path.read_text(encoding="utf-8")
                    async with conn.transaction():
                        await conn.execute(sql)
                        await conn.execute(
                            "INSERT INTO schema_migrations(filename) VALUES ($1)",
                            filename,
                        )
            finally:
                await conn.execute("SELECT pg_advisory_unlock(hashtext('mexc_exhaustion_schema_migrations'))")

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

    async def earliest_candle_time(self, symbol: str, interval: str) -> datetime | None:
        return await self.pool.fetchval(
            "SELECT min(open_time) FROM candles WHERE symbol=$1 AND interval=$2",
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

    async def unresolved_profit_target_symbols(self) -> set[str]:
        """Symbols whose +20%-vs-cross-breach race is still unresolved."""
        rows = await self.pool.fetch(
            """
            SELECT DISTINCT symbol
            FROM shadow_trades
            WHERE target_20_at IS NULL
              AND cross_400_breach_at IS NULL
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
            SET closed_at=$2::timestamptz,
                updated_at=now(),
                metadata = metadata || jsonb_build_object('closed_reason', $3::text)
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

    async def sync_research_signal_snapshots(self) -> int:
        """Backfill frozen confirmed-signal features from data already in PostgreSQL."""
        result = await self.pool.execute(
            """
            INSERT INTO research_signal_features (
                episode_id, symbol, confirmed_at, entry_price, risk_tier,
                run_score, exhaustion_score, episode_started_at, peak_at,
                breakdown_at, retest_at, feature_snapshot, reasons
            )
            SELECT DISTINCT ON (rs.episode_id)
                rs.episode_id,
                rs.symbol,
                rs.signaled_at,
                st.entry_price,
                COALESCE(st.risk_tier, rs.features->>'risk_tier'),
                COALESCE(NULLIF(rs.features->>'run_score', '')::integer, rs.score),
                NULLIF(rs.features->>'exhaustion_score', '')::integer,
                pe.started_at,
                pe.peak_at,
                COALESCE(pe.breakdown_at, NULLIF(rs.features->>'breakdown_at', '')::timestamptz),
                COALESCE(pe.retest_at, NULLIF(rs.features->>'retest_at', '')::timestamptz),
                rs.features,
                rs.reasons
            FROM run_signals rs
            JOIN pump_episodes pe ON pe.id = rs.episode_id
            LEFT JOIN shadow_trades st ON st.episode_id = rs.episode_id
            WHERE rs.level = 'confirmed_short'
              AND rs.episode_id IS NOT NULL
            ORDER BY rs.episode_id, rs.signaled_at ASC
            ON CONFLICT (episode_id) DO NOTHING
            """
        )
        try:
            return int(result.rsplit(" ", 1)[-1])
        except (TypeError, ValueError):
            return 0

    async def sync_research_signal_paths(
        self,
        *,
        batch_rows: int,
        horizon_hours: int,
        statement_timeout_seconds: int,
    ) -> int:
        """Persist a bounded batch of 15m post-signal candles already stored locally.

        No MEXC/API calls are made here. A research-specific PostgreSQL statement
        timeout prevents this optional backfill from competing with scanner hot paths.
        """
        query = """
            WITH bounds AS (
                SELECT
                    st.episode_id,
                    st.symbol,
                    st.confirmed_at,
                    st.entry_price,
                    COALESCE(
                        rp.last_recorded_close,
                        st.confirmed_at - interval '1 microsecond'
                    ) AS last_recorded_close
                FROM shadow_trades st
                LEFT JOIN LATERAL (
                    SELECT max(candle_close_at) AS last_recorded_close
                    FROM research_signal_path_15m
                    WHERE episode_id = st.episode_id
                ) rp ON true
            ),
            candidates AS (
                SELECT
                    b.episode_id,
                    b.symbol,
                    c.open_time + interval '15 minutes' AS candle_close_at,
                    c.open, c.high, c.low, c.close, c.volume, c.amount,
                    (b.entry_price - c.close) / b.entry_price AS close_return_pct,
                    (b.entry_price - c.low) / b.entry_price AS favorable_return_pct,
                    (b.entry_price - c.high) / b.entry_price AS adverse_return_pct,
                    btc.close AS btc_close
                FROM bounds b
                JOIN candles c
                  ON c.symbol = b.symbol
                 AND c.interval = 'Min15'
                 AND c.open_time + interval '15 minutes' > b.last_recorded_close
                 AND c.open_time + interval '15 minutes' > b.confirmed_at
                 AND c.open_time + interval '15 minutes' <= now()
                 AND c.open_time + interval '15 minutes' <=
                     b.confirmed_at + ($2::double precision * interval '1 hour')
                LEFT JOIN candles btc
                  ON btc.symbol = 'BTC_USDT'
                 AND btc.interval = 'Min15'
                 AND btc.open_time = c.open_time
                ORDER BY b.confirmed_at ASC, c.open_time ASC
                LIMIT $1
            ),
            inserted AS (
                INSERT INTO research_signal_path_15m (
                    episode_id, symbol, candle_close_at, open, high, low, close,
                    volume, amount, close_return_pct, favorable_return_pct,
                    adverse_return_pct, btc_close
                )
                SELECT
                    episode_id, symbol, candle_close_at, open, high, low, close,
                    volume, amount, close_return_pct, favorable_return_pct,
                    adverse_return_pct, btc_close
                FROM candidates
                ON CONFLICT (episode_id, candle_close_at) DO NOTHING
                RETURNING 1
            )
            SELECT count(*)::integer FROM inserted
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('statement_timeout', $1, true)",
                    f"{statement_timeout_seconds}s",
                )
                value = await conn.fetchval(query, batch_rows, horizon_hours)
        return int(value or 0)

    async def research_signal_counts(self) -> dict[str, int | datetime | None]:
        row = await self.pool.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM research_signal_features) AS feature_rows,
                (SELECT count(*) FROM research_signal_path_15m) AS path_rows,
                (SELECT max(candle_close_at) FROM research_signal_path_15m) AS last_path_at
            """
        )
        return dict(row) if row is not None else {
            "feature_rows": 0,
            "path_rows": 0,
            "last_path_at": None,
        }

    async def research_analytics_rows(self) -> list[dict[str, Any]]:
        """Return one aggregated research row per public confirmed-short signal.

        The research path can extend to 14 days, while legacy 7-day metrics remain
        explicitly bounded to the first 168h. All work is PostgreSQL-only.
        """
        rows = await self.pool.fetch(
            """
            WITH path_targets AS (
                SELECT
                    p.episode_id,
                    min(p.candle_close_at) FILTER (WHERE p.favorable_return_pct >= 0.05)
                        AS target_5_path_at,
                    min(p.candle_close_at) FILTER (WHERE p.favorable_return_pct >= 0.20)
                        AS target_20_path_at
                FROM research_signal_path_15m p
                JOIN research_signal_features f ON f.episode_id = p.episode_id
                WHERE p.candle_close_at > f.confirmed_at
                GROUP BY p.episode_id
            ),
            path_summary AS (
                SELECT
                    p.episode_id,
                    count(*)::integer AS path_rows,
                    (count(*) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '168 hours'
                    ))::integer AS path_rows_7d,
                    (count(*) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '336 hours'
                    ))::integer AS path_rows_14d,
                    max(p.candle_close_at) AS path_last_at,
                    max(p.favorable_return_pct) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '168 hours'
                    ) AS path_mfe_7d,
                    min(p.adverse_return_pct) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '168 hours'
                    ) AS path_mae_7d,
                    min(p.adverse_return_pct) FILTER (
                        WHERE t.target_5_path_at IS NOT NULL
                          AND p.candle_close_at < t.target_5_path_at
                    ) AS path_mae_before_target_5,
                    (array_agg(
                        p.candle_close_at
                        ORDER BY p.adverse_return_pct ASC, p.candle_close_at ASC
                    ) FILTER (
                        WHERE t.target_5_path_at IS NOT NULL
                          AND p.candle_close_at < t.target_5_path_at
                    ))[1] AS path_mae_before_target_5_at,
                    min(p.adverse_return_pct) FILTER (
                        WHERE t.target_20_path_at IS NOT NULL
                          AND p.candle_close_at <= t.target_20_path_at
                    ) AS path_mae_before_target_20,
                    (array_agg(
                        p.candle_close_at
                        ORDER BY p.favorable_return_pct DESC, p.candle_close_at ASC
                    ) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '168 hours'
                    ))[1] AS path_mfe_at,
                    (array_agg(
                        p.candle_close_at
                        ORDER BY p.adverse_return_pct ASC, p.candle_close_at ASC
                    ) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '168 hours'
                    ))[1] AS path_mae_at,
                    max(p.favorable_return_pct) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '336 hours'
                    ) AS path_mfe_14d,
                    min(p.adverse_return_pct) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '336 hours'
                    ) AS path_mae_14d,
                    (array_agg(
                        p.candle_close_at
                        ORDER BY p.favorable_return_pct DESC, p.candle_close_at ASC
                    ) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '336 hours'
                    ))[1] AS path_mfe_14d_at,
                    (array_agg(
                        p.candle_close_at
                        ORDER BY p.adverse_return_pct ASC, p.candle_close_at ASC
                    ) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '336 hours'
                    ))[1] AS path_mae_14d_at,
                    (array_agg(p.close_return_pct ORDER BY p.candle_close_at DESC) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '24 hours'
                    ))[1] AS path_return_24h,
                    (array_agg(p.close_return_pct ORDER BY p.candle_close_at DESC) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '48 hours'
                    ))[1] AS path_return_48h,
                    (array_agg(p.close_return_pct ORDER BY p.candle_close_at DESC) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '72 hours'
                    ))[1] AS path_return_72h,
                    (array_agg(p.close_return_pct ORDER BY p.candle_close_at DESC) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '96 hours'
                    ))[1] AS path_return_96h,
                    (array_agg(p.close_return_pct ORDER BY p.candle_close_at DESC) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '120 hours'
                    ))[1] AS path_return_120h,
                    (array_agg(p.close_return_pct ORDER BY p.candle_close_at DESC) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '144 hours'
                    ))[1] AS path_return_144h,
                    (array_agg(p.close_return_pct ORDER BY p.candle_close_at DESC) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '168 hours'
                    ))[1] AS path_return_168h,
                    (array_agg(p.close_return_pct ORDER BY p.candle_close_at DESC) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '192 hours'
                    ))[1] AS path_return_192h,
                    (array_agg(p.close_return_pct ORDER BY p.candle_close_at DESC) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '240 hours'
                    ))[1] AS path_return_240h,
                    (array_agg(p.close_return_pct ORDER BY p.candle_close_at DESC) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '288 hours'
                    ))[1] AS path_return_288h,
                    (array_agg(p.close_return_pct ORDER BY p.candle_close_at DESC) FILTER (
                        WHERE p.candle_close_at <= f.confirmed_at + interval '336 hours'
                    ))[1] AS path_return_336h,
                    (array_agg(p.close_return_pct ORDER BY p.candle_close_at DESC))[1] AS path_latest_return,
                    min(p.candle_close_at) FILTER (WHERE p.adverse_return_pct <= -0.10) AS adverse_10_at,
                    min(p.candle_close_at) FILTER (WHERE p.adverse_return_pct <= -0.20) AS adverse_20_at,
                    min(p.candle_close_at) FILTER (WHERE p.adverse_return_pct <= -0.30) AS adverse_30_at,
                    min(p.candle_close_at) FILTER (WHERE p.adverse_return_pct <= -0.50) AS adverse_50_at,
                    min(p.candle_close_at) FILTER (WHERE p.adverse_return_pct <= -0.75) AS adverse_75_at,
                    min(p.candle_close_at) FILTER (WHERE p.adverse_return_pct <= -1.00) AS adverse_100_at,
                    t.target_5_path_at AS target_5_at,
                    min(p.candle_close_at) FILTER (WHERE p.favorable_return_pct >= 0.10) AS target_10_at,
                    min(p.candle_close_at) FILTER (WHERE p.favorable_return_pct >= 0.15) AS target_15_at,
                    t.target_20_path_at,
                    min(p.candle_close_at) FILTER (WHERE p.favorable_return_pct >= 0.25) AS target_25_at,
                    min(p.candle_close_at) FILTER (WHERE p.favorable_return_pct >= 0.30) AS target_30_at,
                    min(p.candle_close_at) FILTER (WHERE p.favorable_return_pct >= 0.40) AS target_40_at
                FROM research_signal_path_15m p
                JOIN research_signal_features f ON f.episode_id = p.episode_id
                LEFT JOIN path_targets t ON t.episode_id = p.episode_id
                WHERE p.candle_close_at > f.confirmed_at
                GROUP BY p.episode_id, f.confirmed_at, t.target_5_path_at, t.target_20_path_at
            )
            SELECT
                f.episode_id, f.symbol, f.confirmed_at, f.entry_price, f.risk_tier,
                f.run_score, f.exhaustion_score, f.episode_started_at, f.peak_at,
                f.breakdown_at, f.retest_at, f.feature_snapshot, f.reasons,
                f.hours_run_to_breakdown, f.hours_breakdown_to_retest,
                f.hours_breakdown_to_confirmation, f.hours_episode_to_confirmation,
                st.current_return_pct, st.return_24h_pct, st.return_48h_pct, st.return_72h_pct, st.return_168h_pct,
                st.first_profit_at, st.target_20_at, st.isolated_100_breach_at,
                st.adverse_200_breach_at, st.adverse_300_breach_at, st.cross_400_breach_at,
                ps.path_rows, ps.path_rows_7d, ps.path_rows_14d, ps.path_last_at,
                ps.path_mfe_7d, ps.path_mae_7d, ps.path_mae_before_target_5, ps.path_mae_before_target_5_at,
                ps.path_mae_before_target_20,
                ps.path_mfe_at, ps.path_mae_at,
                ps.path_mfe_14d, ps.path_mae_14d, ps.path_mfe_14d_at, ps.path_mae_14d_at,
                ps.path_return_24h, ps.path_return_48h, ps.path_return_72h,
                ps.path_return_96h, ps.path_return_120h, ps.path_return_144h,
                ps.path_return_168h, ps.path_return_192h, ps.path_return_240h,
                ps.path_return_288h, ps.path_return_336h, ps.path_latest_return,
                ps.adverse_10_at, ps.adverse_20_at, ps.adverse_30_at,
                ps.adverse_50_at, ps.adverse_75_at, ps.adverse_100_at,
                ps.target_5_at, ps.target_10_at, ps.target_15_at, ps.target_20_path_at,
                ps.target_25_at, ps.target_30_at, ps.target_40_at
            FROM research_signal_features_enriched f
            LEFT JOIN shadow_trades st ON st.episode_id = f.episode_id
            LEFT JOIN path_summary ps ON ps.episode_id = f.episode_id
            WHERE f.risk_tier IN ('standard', 'high_risk')
            ORDER BY f.confirmed_at ASC
            """
        )
        return [dict(row) for row in rows]

    async def research_regime_history_requirements(self, *, lookback_days: int) -> list[dict[str, Any]]:
        """Return the earliest 4h history start required per research signal symbol.

        The requirement is based on each symbol's oldest public confirmed-short
        signal. BTC is included as the common market benchmark.
        """
        rows = await self.pool.fetch(
            """
            WITH public_signals AS (
                SELECT symbol, confirmed_at
                FROM research_signal_features
                WHERE risk_tier IN ('standard', 'high_risk')
            ), requirements AS (
                SELECT
                    symbol,
                    min(confirmed_at) - ($1::int * interval '1 day') AS required_start
                FROM public_signals
                GROUP BY symbol
                UNION ALL
                SELECT
                    'BTC_USDT' AS symbol,
                    min(confirmed_at) - ($1::int * interval '1 day') AS required_start
                FROM public_signals
            )
            SELECT symbol, min(required_start) AS required_start
            FROM requirements
            WHERE required_start IS NOT NULL
            GROUP BY symbol
            ORDER BY symbol
            """,
            lookback_days,
        )
        return [dict(row) for row in rows]

    async def research_regime_history_rows(
        self,
        *,
        lookback_days: int,
        statement_timeout_seconds: int,
    ) -> list[dict[str, Any]]:
        """Return paired completed 4h token/BTC closes strictly before each signal.

        This is the source for v1.3.7 token-behaviour research. The time predicate
        is deliberately signal-relative to avoid post-signal/look-ahead leakage.
        """
        query = """
            SELECT
                f.episode_id, f.symbol, f.confirmed_at, c.open_time,
                c.close AS token_close, btc.close AS btc_close
            FROM research_signal_features f
            JOIN candles c
              ON c.symbol = f.symbol
             AND c.interval = 'Hour4'
             AND c.open_time >= f.confirmed_at - ($1::int * interval '1 day')
             AND c.open_time + interval '4 hours' <= f.confirmed_at
            JOIN candles btc
              ON btc.symbol = 'BTC_USDT'
             AND btc.interval = 'Hour4'
             AND btc.open_time = c.open_time
             AND btc.open_time + interval '4 hours' <= f.confirmed_at
            WHERE f.risk_tier IN ('standard', 'high_risk')
            ORDER BY f.episode_id ASC, c.open_time ASC
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('statement_timeout', $1, true)",
                    f"{statement_timeout_seconds}s",
                )
                rows = await conn.fetch(query, lookback_days)
        return [dict(row) for row in rows]


    async def research_portfolio_path_rows(
        self,
        *,
        statement_timeout_seconds: int,
    ) -> list[dict[str, Any]]:
        """Return stored 15m close marks needed for research-only portfolio MTM replay.

        Bounded to the first seven days after confirmation because every v1.3.5
        champion/challenger portfolio in the paired comparison exits by then.
        PostgreSQL-only; no exchange/API calls.
        """
        query = """
            SELECT
                p.episode_id, p.candle_close_at, p.close_return_pct
            FROM research_signal_path_15m p
            JOIN research_signal_features f ON f.episode_id = p.episode_id
            WHERE f.risk_tier IN ('standard', 'high_risk')
              AND p.candle_close_at > f.confirmed_at
              AND p.candle_close_at <= f.confirmed_at + interval '168 hours'
            ORDER BY p.candle_close_at ASC, p.episode_id ASC
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('statement_timeout', $1, true)",
                    f"{statement_timeout_seconds}s",
                )
                rows = await conn.fetch(query)
        return [dict(row) for row in rows]


    async def research_delayed_entry_rows(
        self,
        *,
        statement_timeout_seconds: int,
    ) -> list[dict[str, Any]]:
        """Simulate delayed entries from stored 15m paths, with a 7-day hold per entry.

        Delay zero uses the original confirmed price/time. Delayed variants enter on
        the first stored 15m close at or after the requested delay. No exchange calls.
        """
        query = """
            WITH delays(delay_minutes) AS (
                VALUES (0), (15), (30), (60), (120), (240), (480)
            ),
            entries AS (
                SELECT
                    f.episode_id,
                    f.symbol,
                    f.risk_tier,
                    d.delay_minutes,
                    CASE WHEN d.delay_minutes = 0 THEN f.confirmed_at ELSE ec.candle_close_at END AS entry_at,
                    CASE WHEN d.delay_minutes = 0 THEN f.entry_price ELSE ec.close END AS delayed_entry_price
                FROM research_signal_features f
                CROSS JOIN delays d
                LEFT JOIN LATERAL (
                    SELECT p.candle_close_at, p.close
                    FROM research_signal_path_15m p
                    WHERE p.episode_id = f.episode_id
                      AND p.candle_close_at >= f.confirmed_at + (d.delay_minutes * interval '1 minute')
                    ORDER BY p.candle_close_at ASC
                    LIMIT 1
                ) ec ON d.delay_minutes > 0
                WHERE f.risk_tier IN ('standard', 'high_risk')
                  AND f.entry_price IS NOT NULL
                  AND f.entry_price > 0
                  AND (d.delay_minutes = 0 OR ec.close IS NOT NULL)
            ),
            summarized AS (
                SELECT
                    e.episode_id,
                    e.symbol,
                    e.risk_tier,
                    e.delay_minutes,
                    e.entry_at,
                    e.delayed_entry_price,
                    count(p.candle_close_at)::integer AS path_rows,
                    max(p.candle_close_at) AS path_last_at,
                    (array_agg(
                        (e.delayed_entry_price - p.close) / e.delayed_entry_price
                        ORDER BY p.candle_close_at DESC
                    ) FILTER (WHERE p.candle_close_at IS NOT NULL))[1] AS return_7d_pct,
                    max((e.delayed_entry_price - p.low) / e.delayed_entry_price) AS mfe_7d,
                    min((e.delayed_entry_price - p.high) / e.delayed_entry_price) AS mae_7d,
                    min(p.candle_close_at) FILTER (
                        WHERE (e.delayed_entry_price - p.low) / e.delayed_entry_price >= 0.20
                    ) AS target_20_at
                FROM entries e
                LEFT JOIN research_signal_path_15m p
                  ON p.episode_id = e.episode_id
                 AND p.candle_close_at > e.entry_at
                 AND p.candle_close_at <= e.entry_at + interval '168 hours'
                GROUP BY
                    e.episode_id, e.symbol, e.risk_tier, e.delay_minutes,
                    e.entry_at, e.delayed_entry_price
            )
            SELECT
                s.*,
                CASE
                    WHEN s.path_last_at IS NULL THEN false
                    ELSE s.path_last_at >= s.entry_at + interval '167 hours 45 minutes'
                         AND s.path_rows >= 659
                END AS path_complete_7d
            FROM summarized s
            ORDER BY s.episode_id, s.delay_minutes
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('statement_timeout', $1, true)",
                    f"{statement_timeout_seconds}s",
                )
                rows = await conn.fetch(query)
        return [dict(row) for row in rows]

    async def fetch_shadow_trades(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT episode_id, symbol, confirmed_at, entry_price, risk_tier, current_price,
                   current_return_pct, last_observed_at, mfe_pct, mae_pct,
                   return_1h_pct, return_4h_pct, return_12h_pct, return_24h_pct,
                   return_48h_pct, return_72h_pct, return_168h_pct,
                   matured_at, matured_48h_at, matured_72h_at, matured_168h_at,
                   first_profit_at, target_20_at, isolated_100_breach_at,
                   adverse_200_breach_at, adverse_300_breach_at, cross_400_breach_at
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
              AND open_time >= $2::timestamptz - interval '15 minutes'
              AND open_time < $3::timestamptz
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

    async def trade_path_events(
        self,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
        entry_price: float,
    ) -> dict[str, datetime | None]:
        """First observed 15m close-time for key path events.

        These are intentionally theoretical research thresholds, not exchange
        liquidation prices. Adverse thresholds map short loss to price multiples:
        -100% => 2x entry, -200% => 3x, -300% => 4x, -400% => 5x.
        """
        row = await self.pool.fetchrow(
            """
            SELECT
                min(open_time + interval '15 minutes') FILTER (WHERE low < $4::double precision) AS first_profit_at,
                min(open_time + interval '15 minutes') FILTER (WHERE low <= $4::double precision * 0.80) AS target_20_at,
                min(open_time + interval '15 minutes') FILTER (WHERE high >= $4::double precision * 2.0) AS isolated_100_breach_at,
                min(open_time + interval '15 minutes') FILTER (WHERE high >= $4::double precision * 3.0) AS adverse_200_breach_at,
                min(open_time + interval '15 minutes') FILTER (WHERE high >= $4::double precision * 4.0) AS adverse_300_breach_at,
                min(open_time + interval '15 minutes') FILTER (WHERE high >= $4::double precision * 5.0) AS cross_400_breach_at
            FROM candles
            WHERE symbol=$1
              AND interval='Min15'
              AND open_time >= $2::timestamptz - interval '15 minutes'
              AND open_time < $3::timestamptz
            """,
            symbol,
            start_at,
            end_at,
            entry_price,
        )
        if row is None:
            return {
                "first_profit_at": None,
                "target_20_at": None,
                "isolated_100_breach_at": None,
                "adverse_200_breach_at": None,
                "adverse_300_breach_at": None,
                "cross_400_breach_at": None,
            }
        return {key: row[key] for key in (
            "first_profit_at",
            "target_20_at",
            "isolated_100_breach_at",
            "adverse_200_breach_at",
            "adverse_300_breach_at",
            "cross_400_breach_at",
        )}

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
              AND open_time + interval '15 minutes' >= $2::timestamptz
              AND open_time + interval '15 minutes' <= $2::timestamptz + interval '30 minutes'
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
        return_48h_pct: float | None = None,
        return_72h_pct: float | None = None,
        return_168h_pct: float | None = None,
        matured_at: datetime | None = None,
        matured_48h_at: datetime | None = None,
        matured_72h_at: datetime | None = None,
        matured_168h_at: datetime | None = None,
        first_profit_at: datetime | None = None,
        target_20_at: datetime | None = None,
        isolated_100_breach_at: datetime | None = None,
        adverse_200_breach_at: datetime | None = None,
        adverse_300_breach_at: datetime | None = None,
        cross_400_breach_at: datetime | None = None,
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
                return_48h_pct = COALESCE(return_48h_pct, $11),
                return_72h_pct = COALESCE(return_72h_pct, $12),
                return_168h_pct = COALESCE(return_168h_pct, $13),
                matured_at = COALESCE(matured_at, $14),
                matured_48h_at = COALESCE(matured_48h_at, $15),
                matured_72h_at = COALESCE(matured_72h_at, $16),
                matured_168h_at = COALESCE(matured_168h_at, $17),
                first_profit_at = COALESCE(first_profit_at, $18),
                target_20_at = COALESCE(target_20_at, $19),
                isolated_100_breach_at = COALESCE(isolated_100_breach_at, $20),
                adverse_200_breach_at = COALESCE(adverse_200_breach_at, $21),
                adverse_300_breach_at = COALESCE(adverse_300_breach_at, $22),
                cross_400_breach_at = COALESCE(cross_400_breach_at, $23),
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
            return_48h_pct,
            return_72h_pct,
            return_168h_pct,
            matured_at,
            matured_48h_at,
            matured_72h_at,
            matured_168h_at,
            first_profit_at,
            target_20_at,
            isolated_100_breach_at,
            adverse_200_breach_at,
            adverse_300_breach_at,
            cross_400_breach_at,
        )

    async def last_performance_report_date(self):
        return await self.pool.fetchval("SELECT max(report_date) FROM performance_reports")

    async def performance_rows(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            WITH tp5_target AS (
                SELECT
                    episode_id,
                    min(candle_close_at) FILTER (WHERE favorable_return_pct >= 0.05) AS target_5_at
                FROM research_signal_path_15m
                GROUP BY episode_id
            ),
            tp5_path AS (
                SELECT
                    p.episode_id,
                    t.target_5_at,
                    min(p.adverse_return_pct) FILTER (
                        WHERE t.target_5_at IS NOT NULL
                          AND p.candle_close_at < t.target_5_at
                    ) AS path_mae_before_target_5,
                    (array_agg(
                        p.candle_close_at
                        ORDER BY p.adverse_return_pct ASC, p.candle_close_at ASC
                    ) FILTER (
                        WHERE t.target_5_at IS NOT NULL
                          AND p.candle_close_at < t.target_5_at
                    ))[1] AS path_mae_before_target_5_at
                FROM research_signal_path_15m p
                LEFT JOIN tp5_target t ON t.episode_id = p.episode_id
                GROUP BY p.episode_id, t.target_5_at
            )
            SELECT st.episode_id, st.symbol, st.confirmed_at, st.entry_price, st.risk_tier,
                   st.current_return_pct, st.mfe_pct, st.mae_pct,
                   st.return_1h_pct, st.return_4h_pct, st.return_12h_pct, st.return_24h_pct,
                   st.return_48h_pct, st.return_72h_pct, st.return_168h_pct,
                   st.matured_at, st.matured_48h_at, st.matured_72h_at, st.matured_168h_at,
                   st.first_profit_at, st.target_20_at, st.isolated_100_breach_at,
                   st.adverse_200_breach_at, st.adverse_300_breach_at, st.cross_400_breach_at,
                   tp.target_5_at, tp.path_mae_before_target_5, tp.path_mae_before_target_5_at
            FROM shadow_trades st
            LEFT JOIN tp5_path tp ON tp.episode_id = st.episode_id
            ORDER BY st.confirmed_at ASC
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

    async def worker_heartbeat(self, worker_name: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            "SELECT worker_name, last_seen_at, status FROM worker_heartbeat WHERE worker_name=$1",
            worker_name,
        )
        return dict(row) if row else None

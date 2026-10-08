from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
from datetime import UTC, datetime, timedelta
from typing import Any

from app.daily_bull_persistence_strategy import daily_bull_persistence_v2_state
from app.daily_core_strategy import daily_confirmed_core_v1_state
from app.daily_regime import reconstruct_daily_regime_features
from app.db import Database
from app.indicators import atr, close_location, ema, pct_return, percentile_rank, upper_wick_ratio
from app.mexc import MexcClient, is_crypto_usdt_contract
from app.models import Candle
from app.signals import (
    ExhaustionFeatures,
    ExhaustionThresholds,
    MarketStateThresholds,
    RunFeatures,
    RunThresholds,
    armed_runner_exhaustion_ready,
    classify_market_state,
    evaluate_failed_retest,
    score_exhaustion,
    score_run,
)
from app.t100_200_strategy import (
    ARMED_RUNNER_MEMORY_HOURS,
    ATR30_PERIOD,
    CONFIRMED_REARM_HOURS,
    DISCOVERY_MIN_CROSS_SECTION_PERCENTILE,
    DISCOVERY_MIN_RETURN_24H,
    EMA_EQ_30_ALPHA,
    EPISODE_MAX_AGE_HOURS,
    EXCLUDED_SYMBOLS,
    FEE_RATE,
    HIGH_RISK_MIN_AMOUNT_24H,
    HIGH_TIER,
    LEGACY_HIGH_TIER,
    MAX_OPEN_POSITIONS,
    MAX_SYMBOLS,
    MIN_AMOUNT_24H,
    PAPER_RUN_ID,
    RETEST_TOLERANCE_ATR,
    RETEST_WINDOW_30M,
    REARM_NEW_HIGH_PCT,
    SHORT_EXHAUSTION_SCORE,
    SLIPPAGE_RATE,
    STRATEGY_ID,
    TRAIL_ACTIVATION_PCT,
    TRAIL_GAP_PCT,
    WIDE_SCAN_MIN_RETURN_72H,
    Stage1Gate,
    evaluate_completed_bar,
    excluded_stablecoin_base,
    notional_fraction,
    short_return_pct,
)
from app.trader_notifier import TraderNotifier

LOGGER = logging.getLogger(__name__)
M30 = timedelta(minutes=30)
H4 = timedelta(hours=4)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )


def _floor_30m(value: datetime) -> datetime:
    value = value.astimezone(UTC).replace(second=0, microsecond=0)
    return value.replace(minute=30 if value.minute >= 30 else 0)


def _ema_alpha(values: list[float], alpha: float, seed_n: int = 5) -> float | None:
    if len(values) < seed_n:
        return None
    current = sum(values[:seed_n]) / seed_n
    for value in values[seed_n:]:
        current = alpha * value + (1.0 - alpha) * current
    return current


def _zscore_current(values: list[float], lookback: int = 48) -> float | None:
    if len(values) < lookback + 1:
        return None
    hist = values[-(lookback + 1):-1]
    current = values[-1]
    mean = sum(hist) / len(hist)
    variance = sum((v - mean) ** 2 for v in hist) / len(hist)
    sd = math.sqrt(variance)
    if sd == 0:
        if current == mean:
            return 0.0
        return 10.0 if current > mean else -10.0
    return (current - mean) / sd


def _candle_row(candle: Candle) -> dict[str, Any]:
    return {
        "open_time": candle.open_time,
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
        "amount": candle.amount,
    }


class T100Worker:
    def __init__(self) -> None:
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise RuntimeError("DATABASE_URL is required")
        if database_url.startswith("postgres://"):
            database_url = "postgresql://" + database_url.removeprefix("postgres://")
        self.log_level = os.getenv("LOG_LEVEL", "INFO")
        self.starting_equity = float(os.getenv("PAPER_STARTING_EQUITY_USDT", "2000"))
        self.poll_seconds = int(os.getenv("T100_POLL_SECONDS", "60"))
        self.request_rate = float(os.getenv("REQUEST_RATE_PER_SECOND", "8"))
        self.request_concurrency = int(os.getenv("REQUEST_CONCURRENCY", "4"))
        self.require_spot_pair = os.getenv("REQUIRE_MEXC_SPOT_PAIR", "true").lower() in {
            "1", "true", "yes", "on"
        }
        mode = os.getenv("TRADING_MODE", "paper").strip().lower()
        if mode != "paper":
            raise RuntimeError("T100_200 promotion is paper/shadow only; TRADING_MODE must be paper")

        self.db = Database(database_url)
        self.mexc = MexcClient(
            os.getenv("MEXC_BASE_URL", "https://contract.mexc.com"),
            spot_base_url=os.getenv("MEXC_SPOT_BASE_URL", "https://api.mexc.com"),
            request_rate_per_second=self.request_rate,
            request_concurrency=self.request_concurrency,
        )
        self.notifier = TraderNotifier(os.getenv("DISCORD_TRADER_EVENTS_WEBHOOK_URL") or None)
        self.stop_event = asyncio.Event()
        self.contracts: set[str] = set()

        self.run_thresholds = RunThresholds(
            min_amount_24h=MIN_AMOUNT_24H,
            max_spread_pct=0.35,
        )
        self.exhaustion_thresholds = ExhaustionThresholds()
        self.state_thresholds = MarketStateThresholds(
            min_run_score=3,
            run_watch_min_24h=0.08,
            run_watch_min_72h=0.20,
            exhaustion_watch_min_72h=0.30,
            exhaustion_watch_min_24h=-0.25,
            exhaustion_watch_max_24h=0.08,
            active_exhaustion_min_score=2,
        )

    async def close(self) -> None:
        await self.notifier.close()
        await self.mexc.close()
        await self.db.close()

    async def initialize(self) -> None:
        await self.db.connect()
        await self.db.migrate()
        await self.db.pool.execute(
            """
            INSERT INTO t100_runtime(
                singleton,strategy_id,run_id,starting_equity_usdt,realized_equity_usdt
            ) VALUES(true,$1,$2,$3,$3)
            ON CONFLICT(singleton) DO NOTHING
            """,
            STRATEGY_ID,
            PAPER_RUN_ID,
            self.starting_equity,
        )
        row = await self.db.pool.fetchrow(
            "SELECT strategy_id,run_id FROM t100_runtime WHERE singleton=true"
        )
        if row is None or row["strategy_id"] != STRATEGY_ID:
            raise RuntimeError("t100_runtime strategy id mismatch; refusing to mix paper runs")
        await self.refresh_contracts()

    async def refresh_contracts(self) -> None:
        rows, spot_assets = await asyncio.gather(
            self.mexc.get_contracts(),
            self.mexc.get_spot_usdt_assets(),
        )
        await self.db.upsert_contracts(rows)
        self.contracts = {
            str(row["symbol"]).upper()
            for row in rows
            if is_crypto_usdt_contract(
                row,
                spot_assets,
                require_spot_pair=self.require_spot_pair,
            )
            and not excluded_stablecoin_base(str(row["symbol"]))
        }
        LOGGER.info("T100 universe refreshed: crypto Min30 symbols=%d", len(self.contracts))

    async def _sync_interval(
        self,
        symbol: str,
        interval: str,
        *,
        bootstrap: timedelta,
        overlap: timedelta,
    ) -> None:
        latest = await self.db.latest_candle_time(symbol, interval)
        now = datetime.now(UTC)
        start = now - bootstrap if latest is None else latest - overlap
        candles = await self.mexc.get_klines(
            symbol,
            interval,
            max(0, int(start.timestamp())),
            int(now.timestamp()),
        )
        await self.db.upsert_candles(candles)

    async def sync_market_history(self) -> None:
        """Refresh native Min30 + Hour4 history for the whole frozen proxy universe."""
        if not self.contracts:
            await self.refresh_contracts()
        semaphore = asyncio.Semaphore(self.request_concurrency)
        symbols = sorted(self.contracts)

        async def one(symbol: str) -> None:
            async with semaphore:
                await self._sync_interval(
                    symbol, "Min30", bootstrap=timedelta(days=10), overlap=timedelta(hours=3)
                )
                await self._sync_interval(
                    symbol, "Hour4", bootstrap=timedelta(days=120), overlap=timedelta(hours=12)
                )

        results = await asyncio.gather(*(one(s) for s in symbols), return_exceptions=True)
        failures = [str(r) for r in results if isinstance(r, Exception)]
        LOGGER.info(
            "T100 candle sync: symbols=%d failures=%d", len(symbols), len(failures)
        )
        if failures:
            LOGGER.warning("T100 candle sync examples: %s", failures[:5])

    async def _feature_for_symbol(
        self,
        symbol: str,
        eval_at: datetime,
        *,
        include_recent_m30: bool = True,
    ) -> dict[str, Any] | None:
        feature_at = eval_at - M30
        m30 = await self.db.fetch_candles(symbol, "Min30", 420)
        completed = [c for c in m30 if c.open_time <= feature_at]
        if len(completed) < 145 or completed[-1].open_time != feature_at:
            return None
        current = completed[-1]
        closes = [c.close for c in completed]
        volumes = [c.volume for c in completed]
        amount24 = sum(c.amount for c in completed[-48:])
        r24 = pct_return(completed[-49].close, current.close)
        r72 = pct_return(completed[-145].close, current.close)
        if r24 is None or r72 is None:
            return None
        atr7 = atr(
            [c.high for c in completed],
            [c.low for c in completed],
            closes,
            ATR30_PERIOD,
        )
        vol_z = _zscore_current(volumes, 48)
        ema_eq = _ema_alpha(closes, EMA_EQ_30_ALPHA, 5)
        peak_slice = completed[-145:]
        peak = max(peak_slice, key=lambda c: (c.high, c.open_time))

        h4 = await self.db.fetch_candles(symbol, "Hour4", 760)
        h4c = [c for c in h4 if c.open_time + H4 <= eval_at]
        if len(h4c) < 25:
            return None
        # Frozen historical proxy computed production-style Hour4 metrics
        # over a rolling window capped at the most recent 80 completed H4 bars.
        h4w = h4c[-80:]
        h4cl = [c.close for c in h4w]
        h4ema = ema(h4cl, 20)
        h4atr = atr(
            [c.high for c in h4w],
            [c.low for c in h4w],
            h4cl,
            14,
        )
        if h4ema is None or h4atr is None or h4atr <= 0:
            return None

        momentum = pct_return(completed[-3].close, current.close)
        previous_momentum = pct_return(completed[-5].close, completed[-3].close)
        support = min(completed[-3].low, completed[-2].low)
        structural = current.close < support
        lower_hc = current.high < completed[-2].high and current.close < completed[-2].close

        row = {
            "symbol": symbol,
            "eval_at": eval_at,
            "feature_at": feature_at,
            "entry_basis": current.close,
            "r24": r24,
            "r72": r72,
            "amount24": amount24,
            "volume_z": vol_z,
            "distance_h4_atr": (current.close - h4ema) / h4atr,
            "atr7": atr7,
            "ema_eq": ema_eq,
            "rolling_peak_price": peak.high,
            "rolling_peak_at": peak.open_time,
            "momentum": momentum,
            "previous_momentum": previous_momentum,
            "support": support,
            "upper_wick": upper_wick_ratio(
                current.open, current.high, current.low, current.close
            ),
            "close_location": close_location(current.high, current.low, current.close),
            "below_ema_eq": ema_eq is not None and current.close < ema_eq,
            "lower_high_and_close": lower_hc,
            "structural_break": structural,
        }
        if include_recent_m30:
            row["current_candle"] = current
            row["recent_m30"] = completed[-220:]
        return row

    async def _active_scanner_symbols(self) -> set[str]:
        rows = await self.db.pool.fetch("SELECT symbol FROM t100_scanner_state")
        return {str(r["symbol"]) for r in rows}

    async def _discovery_feature_for_symbol(
        self, symbol: str, eval_at: datetime
    ) -> dict[str, Any] | None:
        """Return only the scalar fields required for frozen universe ranking.

        This deliberately avoids the full T100 feature builder: discovery ranking
        needs only Min30 r24/r72/amount24. Keeping Hour4, ATR, exhaustion and
        lifecycle candle objects out of the all-symbol pass prevents catch-up
        memory spikes while preserving the exact ranking inputs.
        """
        feature_at = eval_at - M30
        rows = await self.db.pool.fetch(
            """
            SELECT open_time, close, amount
            FROM candles
            WHERE symbol=$1
              AND interval='Min30'
              AND open_time <= $2
            ORDER BY open_time DESC
            LIMIT 145
            """,
            symbol,
            feature_at,
        )
        if len(rows) < 145:
            return None
        rows = list(reversed(rows))
        if rows[-1]["open_time"] != feature_at:
            return None

        current_close = float(rows[-1]["close"])
        r24 = pct_return(float(rows[-49]["close"]), current_close)
        r72 = pct_return(float(rows[-145]["close"]), current_close)
        amount24 = sum(float(row["amount"]) for row in rows[-48:])
        return {
            "symbol": symbol,
            "r24": r24,
            "r72": r72,
            "amount24": amount24,
        }

    async def _select_eval_candidates(
        self, eval_at: datetime
    ) -> list[dict[str, Any]]:
        """Select the frozen proxy universe without retaining candle histories.

        Cross-sectional ranking requires only r24/r72/amount24 for the whole
        universe. Lifecycle processing needs the full feature/candle payload only
        for selected symbols, which the scanner hydrates one at a time.
        """
        semaphore = asyncio.Semaphore(self.request_concurrency)

        async def one(symbol: str):
            async with semaphore:
                return await self._discovery_feature_for_symbol(symbol, eval_at)

        symbols = sorted(self.contracts)
        rows: list[dict[str, Any]] = []
        batch_size = max(8, self.request_concurrency * 4)
        for start in range(0, len(symbols), batch_size):
            batch = symbols[start : start + batch_size]
            results = await asyncio.gather(
                *(one(symbol) for symbol in batch),
                return_exceptions=True,
            )
            rows.extend(r for r in results if isinstance(r, dict))

        returns = [float(r["r24"]) for r in rows]
        btc = next((r for r in rows if r["symbol"] == "BTC_USDT"), None)
        btc_r24 = None if btc is None else float(btc["r24"])
        active = await self._active_scanner_symbols()

        ranked: list[tuple[int, float, float, str, dict[str, Any]]] = []
        for row in rows:
            symbol = str(row["symbol"])
            if symbol in EXCLUDED_SYMBOLS:
                continue
            rank = percentile_rank(float(row["r24"]), returns)
            row["cross_rank"] = rank
            row["btc_r24"] = btc_r24
            is_active = symbol in active
            standard = float(row["amount24"]) >= MIN_AMOUNT_24H
            mover = float(row["r24"]) >= DISCOVERY_MIN_RETURN_24H
            relative = rank is not None and rank >= DISCOVERY_MIN_CROSS_SECTION_PERCENTILE
            mover72 = float(row["r72"]) >= WIDE_SCAN_MIN_RETURN_72H
            if not (is_active or standard or mover or relative or mover72):
                continue
            ranked.append(
                (
                    2 if is_active else (1 if mover72 else 0),
                    max(float(row["r24"]), float(row["r72"])),
                    float(row["amount24"]),
                    symbol,
                    row,
                )
            )

        ranked.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
        selected = [x[4] for x in ranked[:MAX_SYMBOLS]]
        return [
            {
                "symbol": str(row["symbol"]),
                "cross_rank": row.get("cross_rank"),
                "btc_r24": row.get("btc_r24"),
            }
            for row in selected
            if float(row["amount24"]) >= HIGH_RISK_MIN_AMOUNT_24H
        ]

    async def _build_eval_rows(self, eval_at: datetime) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(self.request_concurrency)

        async def one(symbol: str):
            async with semaphore:
                return await self._feature_for_symbol(symbol, eval_at)

        symbols = sorted(self.contracts)
        results = await asyncio.gather(*(one(s) for s in symbols), return_exceptions=True)
        rows = [r for r in results if isinstance(r, dict)]
        returns = [float(r["r24"]) for r in rows]
        btc = next((r for r in rows if r["symbol"] == "BTC_USDT"), None)
        btc_r24 = None if btc is None else float(btc["r24"])
        active = await self._active_scanner_symbols()

        ranked: list[tuple[int, float, float, str, dict[str, Any]]] = []
        for row in rows:
            symbol = str(row["symbol"])
            if symbol in EXCLUDED_SYMBOLS:
                continue
            rank = percentile_rank(float(row["r24"]), returns)
            row["cross_rank"] = rank
            row["btc_r24"] = btc_r24
            is_active = symbol in active
            standard = float(row["amount24"]) >= MIN_AMOUNT_24H
            mover = float(row["r24"]) >= DISCOVERY_MIN_RETURN_24H
            relative = rank is not None and rank >= DISCOVERY_MIN_CROSS_SECTION_PERCENTILE
            mover72 = float(row["r72"]) >= WIDE_SCAN_MIN_RETURN_72H
            if not (is_active or standard or mover or relative or mover72):
                continue
            ranked.append(
                (
                    2 if is_active else (1 if mover72 else 0),
                    max(float(row["r24"]), float(row["r72"])),
                    float(row["amount24"]),
                    symbol,
                    row,
                )
            )
        ranked.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
        selected = [x[4] for x in ranked[:MAX_SYMBOLS]]
        return [r for r in selected if float(r["amount24"]) >= HIGH_RISK_MIN_AMOUNT_24H]

    async def _load_scanner_state(self, symbol: str) -> dict[str, Any] | None:
        row = await self.db.pool.fetchrow(
            "SELECT * FROM t100_scanner_state WHERE symbol=$1", symbol
        )
        return dict(row) if row else None

    async def _save_scanner_state(self, state: dict[str, Any]) -> None:
        await self.db.pool.execute(
            """
            INSERT INTO t100_scanner_state(
                symbol,started_at,state,peak_price,peak_at,last_run_score,last_exhaustion_score,
                broken_level,breakdown_at,breakdown_atr7,confirmed_at,metadata,updated_at
            ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,now())
            ON CONFLICT(symbol) DO UPDATE SET
                started_at=EXCLUDED.started_at,state=EXCLUDED.state,
                peak_price=EXCLUDED.peak_price,peak_at=EXCLUDED.peak_at,
                last_run_score=EXCLUDED.last_run_score,
                last_exhaustion_score=EXCLUDED.last_exhaustion_score,
                broken_level=EXCLUDED.broken_level,breakdown_at=EXCLUDED.breakdown_at,
                breakdown_atr7=EXCLUDED.breakdown_atr7,confirmed_at=EXCLUDED.confirmed_at,
                metadata=EXCLUDED.metadata,updated_at=now()
            """,
            state["symbol"], state["started_at"], state["state"], state["peak_price"],
            state["peak_at"], state["last_run_score"], state["last_exhaustion_score"],
            state.get("broken_level"), state.get("breakdown_at"), state.get("breakdown_atr7"),
            state.get("confirmed_at"), json.dumps(state.get("metadata", {}), default=str),
        )

    async def _delete_scanner_state(self, symbol: str) -> None:
        await self.db.pool.execute("DELETE FROM t100_scanner_state WHERE symbol=$1", symbol)

    async def _daily_filter(
        self, symbol: str, confirmed_at: datetime, entry: float, features: dict[str, Any]
    ) -> tuple[bool, str]:
        try:
            await self._sync_interval(
                symbol, "Day1", bootstrap=timedelta(days=60), overlap=timedelta(days=3)
            )
        except Exception:
            LOGGER.warning("T100 Day1 refresh failed for %s", symbol, exc_info=True)
        day1 = await self.db.fetch_candles(symbol, "Day1", 60)
        recovered, _ = reconstruct_daily_regime_features(
            confirmed_at=confirmed_at,
            entry_price=entry,
            day1_rows=[_candle_row(c) for c in day1],
        )
        features.update(recovered)
        core = daily_confirmed_core_v1_state(features)
        if core is None:
            return False, "missing_daily_core"
        if core:
            return False, "daily_core_skip"
        persistence = daily_bull_persistence_v2_state(features)
        if persistence is None:
            return False, "missing_persistence"
        if persistence:
            return False, "persistence_skip"
        return True, "admitted"

    async def _strict365(self, symbol: str, feature_at: datetime) -> bool:
        earliest = await self.db.earliest_candle_time(symbol, "Min30")
        needed = feature_at - timedelta(days=365)
        if earliest is None or earliest > needed:
            # This is an intentional deep backfill, not the normal overlap sync.
            # get_klines paginates the native Min30 endpoint in <=1900-bar windows.
            try:
                start = feature_at - timedelta(days=370)
                candles = await self.mexc.get_klines(
                    symbol,
                    "Min30",
                    max(0, int(start.timestamp())),
                    int(feature_at.timestamp()),
                )
                await self.db.upsert_candles(candles)
            except Exception:
                LOGGER.warning("T100 strict365 backfill failed for %s", symbol, exc_info=True)
            earliest = await self.db.earliest_candle_time(symbol, "Min30")
        return earliest is not None and earliest <= needed

    async def _reset_between(
        self, symbol: str, previous_feature: datetime, current_feature: datetime
    ) -> bool:
        if current_feature <= previous_feature + M30:
            return False
        start = previous_feature - timedelta(hours=72)
        rows = await self.db.pool.fetch(
            """
            SELECT open_time,close FROM candles
            WHERE symbol=$1 AND interval='Min30'
              AND open_time >= $2 AND open_time <= $3
            ORDER BY open_time
            """,
            symbol, start, current_feature,
        )
        by_time = {r["open_time"]: float(r["close"]) for r in rows}
        for t, close in sorted(by_time.items()):
            if not (previous_feature < t < current_feature):
                continue
            old = by_time.get(t - timedelta(hours=72))
            if old is not None and old > 0 and close / old - 1.0 <= 0:
                return True
        return False

    async def _record_p2(
        self,
        *,
        symbol: str,
        p2_at: datetime,
        feature_at: datetime,
        entry: float,
        risk_tier: str,
        feature_r24: float,
        atr7: float | None,
        features: dict[str, Any],
    ) -> dict[str, Any] | None:
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow(
                    "SELECT * FROM t100_p2_signals WHERE symbol=$1 AND p2_at=$2",
                    symbol, p2_at,
                )
                if existing:
                    return dict(existing)

                prior = await conn.fetchrow(
                    "SELECT * FROM t100_stage_state WHERE symbol=$1 FOR UPDATE", symbol
                )
                reset = False
                if prior is not None:
                    reset = await self._reset_between(
                        symbol, prior["previous_feature_at"], feature_at
                    )

                if prior is None or reset:
                    episode_seq = 1 if prior is None else int(prior["episode_seq"]) + 1
                    stage_no = 1
                    strict365 = await self._strict365(symbol, feature_at)
                    atr_pct = None if atr7 is None or entry <= 0 else atr7 / entry * 100.0
                    gate = Stage1Gate(strict365, feature_r24, atr_pct)
                    tier = gate.tier
                    await conn.execute(
                        """
                        INSERT INTO t100_stage_state(
                            symbol,episode_seq,previous_p2_at,previous_feature_at,stage_no,
                            stage1_p2_at,stage1_feature_at,stage1_entry_price,
                            stage1_return_24h,stage1_atr7_pct,stage1_strict365,
                            stage1_eligible,t100_tier,metadata,updated_at
                        ) VALUES($1,$2,$3,$4,1,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,now())
                        ON CONFLICT(symbol) DO UPDATE SET
                            episode_seq=EXCLUDED.episode_seq,previous_p2_at=EXCLUDED.previous_p2_at,
                            previous_feature_at=EXCLUDED.previous_feature_at,stage_no=1,
                            stage1_p2_at=EXCLUDED.stage1_p2_at,
                            stage1_feature_at=EXCLUDED.stage1_feature_at,
                            stage1_entry_price=EXCLUDED.stage1_entry_price,
                            stage1_return_24h=EXCLUDED.stage1_return_24h,
                            stage1_atr7_pct=EXCLUDED.stage1_atr7_pct,
                            stage1_strict365=EXCLUDED.stage1_strict365,
                            stage1_eligible=EXCLUDED.stage1_eligible,t100_tier=EXCLUDED.t100_tier,
                            metadata=EXCLUDED.metadata,updated_at=now()
                        """,
                        symbol, episode_seq, p2_at, feature_at, entry, feature_r24, atr_pct,
                        strict365, gate.eligible, tier,
                        json.dumps({"reset": reset}, default=str),
                    )
                    stage1_r24 = feature_r24
                    stage1_atr_pct = atr_pct
                    stage1_strict = strict365
                    stage1_eligible = gate.eligible
                    t100_tier = tier
                else:
                    episode_seq = int(prior["episode_seq"])
                    stage_no = int(prior["stage_no"]) + 1
                    stage1_r24 = prior["stage1_return_24h"]
                    stage1_atr_pct = prior["stage1_atr7_pct"]
                    stage1_strict = bool(prior["stage1_strict365"])
                    stage1_eligible = bool(prior["stage1_eligible"])
                    t100_tier = prior["t100_tier"]
                    await conn.execute(
                        """
                        UPDATE t100_stage_state
                        SET previous_p2_at=$2,previous_feature_at=$3,stage_no=$4,updated_at=now()
                        WHERE symbol=$1
                        """,
                        symbol, p2_at, feature_at, stage_no,
                    )

                eligible_stage2 = stage_no == 2 and stage1_eligible
                row = await conn.fetchrow(
                    """
                    INSERT INTO t100_p2_signals(
                        strategy_id,symbol,p2_at,feature_at,entry_price,scanner_risk_tier,
                        physical_episode_seq,stage_no,reset_reason,stage1_return_24h,
                        stage1_atr7_pct,stage1_strict365,stage1_eligible,t100_tier,
                        eligible_stage2,features
                    ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16::jsonb)
                    RETURNING *
                    """,
                    STRATEGY_ID, symbol, p2_at, feature_at, entry, risk_tier, episode_seq,
                    stage_no, "r72_nonpositive_inside" if reset else None, stage1_r24,
                    stage1_atr_pct, stage1_strict, stage1_eligible, t100_tier,
                    eligible_stage2, json.dumps(features, default=str),
                )
                return dict(row)

    async def _process_symbol_row(self, row: dict[str, Any]) -> None:
        symbol = str(row["symbol"])
        eval_at = row["eval_at"]
        rf = RunFeatures(
            return_24h=float(row["r24"]),
            return_72h=float(row["r72"]),
            btc_return_24h=row.get("btc_r24"),
            residual_return_24h=(
                float(row["r24"]) - float(row["btc_r24"])
                if row.get("btc_r24") is not None else None
            ),
            cross_section_percentile=row.get("cross_rank"),
            volume_zscore_15m=row.get("volume_z"),
            distance_above_ema20_atr_4h=row.get("distance_h4_atr"),
            amount_24h=float(row["amount24"]),
            spread_pct=None,
            funding_rate=None,
            fair_index_premium_pct=None,
            hold_vol=None,
        )
        run_score, _, scorable = score_run(rf, self.run_thresholds)
        ex = ExhaustionFeatures(
            upper_wick_ratio_15m=row.get("upper_wick"),
            close_location_15m=row.get("close_location"),
            momentum_1h=row.get("momentum"),
            previous_momentum_1h=row.get("previous_momentum"),
            momentum_decelerating=(
                row.get("momentum") is not None
                and row.get("previous_momentum") is not None
                and float(row["momentum"]) < float(row["previous_momentum"])
            ),
            below_ema9_15m=bool(row.get("below_ema_eq")),
            lower_high_and_close=bool(row.get("lower_high_and_close")),
            structural_break_15m=bool(row.get("structural_break")),
            volume_zscore_15m=row.get("volume_z"),
        )
        ex_score, _ = score_exhaustion(ex, self.exhaustion_thresholds)
        state_name, _ = classify_market_state(
            rf, run_score, ex, ex_score, self.state_thresholds
        )
        normal_valid = scorable and state_name is not None
        state = await self._load_scanner_state(symbol)

        if state and eval_at - state["started_at"] > timedelta(hours=EPISODE_MAX_AGE_HOURS):
            await self._delete_scanner_state(symbol)
            state = None

        if state and state["confirmed_at"] is None and row["rolling_peak_price"] > state["peak_price"]:
            state["peak_price"] = float(row["rolling_peak_price"])
            state["peak_at"] = row["rolling_peak_at"]

        if state and state["confirmed_at"] is not None:
            highs = [
                c.high for c in row["recent_m30"]
                if c.open_time > state["confirmed_at"]
            ]
            new_high = max(highs, default=0.0)
            rearm_high = (
                normal_valid
                and new_high >= float(state["peak_price"]) * (1.0 + REARM_NEW_HIGH_PCT)
            )
            rearm_time = (
                normal_valid
                and eval_at - state["confirmed_at"] >= timedelta(hours=CONFIRMED_REARM_HOURS)
            )
            if rearm_high or rearm_time:
                await self._delete_scanner_state(symbol)
                state = None
            else:
                return

        if state and state["state"] == "breakdown_watch":
            if not state["breakdown_at"] or not state["broken_level"] or not state["breakdown_atr7"]:
                state["state"] = state_name or "exhaustion_watch"
                state["breakdown_at"] = state["broken_level"] = state["breakdown_atr7"] = None
            else:
                ret = evaluate_failed_retest(
                    row["recent_m30"],
                    breakdown_at=state["breakdown_at"],
                    broken_level=float(state["broken_level"]),
                    atr_15m=float(state["breakdown_atr7"]),
                    tolerance_atr=RETEST_TOLERANCE_ATR,
                    window_candles=RETEST_WINDOW_30M,
                )
                if ret.confirmed:
                    entry = (
                        float(ret.retest_close)
                        if ret.retest_close is not None and ret.retest_close > 0
                        else float(row["entry_basis"])
                    )
                    risk = "standard" if float(row["amount24"]) >= MIN_AMOUNT_24H else "high_risk"
                    features = rf.as_dict()
                    features.update(ex.as_dict())
                    features.update(
                        {
                            "run_score": run_score,
                            "exhaustion_score": ex_score,
                            "atr_30m_7": row.get("atr7"),
                            "risk_tier": risk,
                            "episode_peak_price": state["peak_price"],
                            "episode_started_at": state["started_at"].isoformat(),
                            "broken_level": state["broken_level"],
                            "breakdown_at": state["breakdown_at"].isoformat(),
                            "hours_run_to_breakdown": max(
                                0.0,
                                (state["breakdown_at"] - state["started_at"]).total_seconds() / 3600.0,
                            ),
                            "retest_at": ret.retest_at.isoformat() if ret.retest_at else None,
                            "retest_high": ret.retest_high,
                            "retest_close": ret.retest_close,
                        }
                    )
                    admitted, why = await self._daily_filter(symbol, eval_at, entry, features)
                    if admitted:
                        signal_row = await self._record_p2(
                            symbol=symbol,
                            p2_at=eval_at,
                            feature_at=row["feature_at"],
                            entry=entry,
                            risk_tier=risk,
                            feature_r24=float(row["r24"]),
                            atr7=row.get("atr7"),
                            features=features,
                        )
                        if signal_row and signal_row["eligible_stage2"]:
                            LOGGER.info(
                                "T100 eligible Stage2 %s tier=%s stage1_r24=%.2f%% atr7=%.2f%%",
                                symbol,
                                signal_row["t100_tier"],
                                float(signal_row["stage1_return_24h"]) * 100.0,
                                float(signal_row["stage1_atr7_pct"]),
                            )
                    else:
                        LOGGER.info("T100 raw confirmation rejected %s: %s", symbol, why)
                    state["confirmed_at"] = eval_at
                    state["state"] = "confirmed_short"
                    await self._save_scanner_state(state)
                    return
                if ret.invalidated or ret.expired:
                    state["state"] = state_name or "exhaustion_watch"
                    state["breakdown_at"] = None
                    state["broken_level"] = None
                    state["breakdown_atr7"] = None
                    if ret.invalidated:
                        await self._save_scanner_state(state)
                        return
                else:
                    await self._save_scanner_state(state)
                    return

        if state and not normal_valid:
            anchor = max(state["started_at"], state["peak_at"])
            if eval_at - anchor > timedelta(hours=ARMED_RUNNER_MEMORY_HOURS):
                await self._delete_scanner_state(symbol)
                return
            if state["state"] == "exhaustion_watch" or armed_runner_exhaustion_ready(
                ex, ex_score, self.state_thresholds
            ):
                state_name = "exhaustion_watch"
                state["state"] = "exhaustion_watch"
            else:
                await self._save_scanner_state(state)
                return

        if state is None and not normal_valid:
            return
        if state is None:
            state = {
                "symbol": symbol,
                "started_at": eval_at,
                "state": str(state_name),
                "peak_price": float(row["rolling_peak_price"]),
                "peak_at": row["rolling_peak_at"],
                "last_run_score": run_score,
                "last_exhaustion_score": ex_score,
                "broken_level": None,
                "breakdown_at": None,
                "breakdown_atr7": None,
                "confirmed_at": None,
                "metadata": {},
            }
        else:
            state["state"] = str(state_name)
            state["last_run_score"] = run_score
            state["last_exhaustion_score"] = ex_score
            if row["rolling_peak_price"] > state["peak_price"]:
                state["peak_price"] = float(row["rolling_peak_price"])
                state["peak_at"] = row["rolling_peak_at"]

        if (
            state_name == "exhaustion_watch"
            and bool(row["structural_break"])
            and ex_score >= SHORT_EXHAUSTION_SCORE
            and row.get("atr7") is not None
            and float(row["atr7"]) > 0
        ):
            state["state"] = "breakdown_watch"
            state["broken_level"] = float(row["support"])
            state["breakdown_at"] = row["feature_at"]
            state["breakdown_atr7"] = float(row["atr7"])
        await self._save_scanner_state(state)

    async def _runtime(self) -> dict[str, Any]:
        row = await self.db.pool.fetchrow("SELECT * FROM t100_runtime WHERE singleton=true")
        if row is None:
            raise RuntimeError("missing t100_runtime")
        return dict(row)

    async def _open_positions(self) -> list[dict[str, Any]]:
        rows = await self.db.pool.fetch(
            "SELECT * FROM t100_positions WHERE status='open' ORDER BY slot_no"
        )
        return [dict(r) for r in rows]

    async def _equity(self, positions: list[dict[str, Any]] | None = None) -> float:
        runtime = await self._runtime()
        positions = positions if positions is not None else await self._open_positions()
        unrealized = sum(
            float(p["notional_usdt"])
            * (float(p["entry_price"]) - float(p["current_price"]))
            / float(p["entry_price"])
            for p in positions
        )
        return float(runtime["realized_equity_usdt"]) + unrealized

    async def _apply_funding(self, bar_start: datetime) -> None:
        positions = await self._open_positions()
        for p in positions:
            symbol = str(p["symbol"])
            try:
                rows = await self.mexc.get_funding_history(symbol, page_size=1000)
                await self.db.upsert_funding_history(symbol, rows)
            except Exception:
                LOGGER.warning("T100 funding refresh failed for %s", symbol, exc_info=True)
            rates = await self.db.pool.fetch(
                """
                SELECT settle_time,funding_rate FROM funding_rates
                WHERE symbol=$1 AND settle_time>$2 AND settle_time<=$3
                ORDER BY settle_time
                """,
                symbol, p["opened_at"], bar_start,
            )
            for rate in rates:
                settle = rate["settle_time"]
                exists = await self.db.pool.fetchval(
                    "SELECT 1 FROM t100_funding_applied WHERE position_id=$1 AND settle_time=$2",
                    p["id"], settle,
                )
                if exists:
                    continue
                value = float(p["notional_usdt"]) * float(p["current_price"]) / float(p["entry_price"])
                pnl = value * float(rate["funding_rate"])
                async with self.db.pool.acquire() as conn:
                    async with conn.transaction():
                        inserted = await conn.fetchval(
                            """
                            INSERT INTO t100_funding_applied(
                                position_id,settle_time,funding_rate,position_value_usdt,pnl_usdt
                            ) VALUES($1,$2,$3,$4,$5)
                            ON CONFLICT DO NOTHING RETURNING 1
                            """,
                            p["id"], settle, float(rate["funding_rate"]), value, pnl,
                        )
                        if not inserted:
                            continue
                        await conn.execute(
                            "UPDATE t100_positions SET funding_pnl_usdt=funding_pnl_usdt+$2 WHERE id=$1",
                            p["id"], pnl,
                        )
                        await conn.execute(
                            """
                            UPDATE t100_runtime
                            SET realized_equity_usdt=realized_equity_usdt+$1,updated_at=now()
                            WHERE singleton=true
                            """,
                            pnl,
                        )

    async def _process_position_bar(self, eval_at: datetime) -> None:
        bar_start = eval_at - M30
        await self._apply_funding(bar_start)
        positions = await self._open_positions()
        for p in positions:
            if p["opened_at"] > bar_start:
                continue
            candle = await self.db.pool.fetchrow(
                """
                SELECT high,low,close FROM candles
                WHERE symbol=$1 AND interval='Min30' AND open_time=$2
                """,
                p["symbol"], bar_start,
            )
            if not candle:
                continue
            decision = evaluate_completed_bar(
                entry=float(p["entry_price"]),
                high=float(candle["high"]),
                low=float(candle["low"]),
                trail_active=bool(p["trail_active"]),
                best_profit_pct=float(p["best_profit_pct"]),
                trail_activation_pct=float(p["trail_activation_pct"]),
                trail_gap_pct=float(p["trail_gap_pct"]),
            )
            mae = max(
                float(p["mae_pct"]),
                (float(candle["high"]) / float(p["entry_price"]) - 1.0) * 100.0,
            )
            mfe = max(
                float(p["mfe_pct"]),
                short_return_pct(float(p["entry_price"]), float(candle["low"])),
            )
            if decision.exit_price is None:
                await self.db.pool.execute(
                    """
                    UPDATE t100_positions SET
                        trail_active=$2,best_profit_pct=$3,current_price=$4,
                        current_return_pct=$5,mae_pct=$6,mfe_pct=$7,updated_at=now()
                    WHERE id=$1
                    """,
                    p["id"], decision.trail_active, decision.best_profit_pct,
                    float(candle["close"]),
                    short_return_pct(float(p["entry_price"]), float(candle["close"])),
                    mae, mfe,
                )
                continue

            exit_px = float(decision.exit_price)
            notional = float(p["notional_usdt"])
            entry = float(p["entry_price"])
            gross = notional * (entry - exit_px) / entry
            exit_value = notional * exit_px / entry
            exit_fee = exit_value * FEE_RATE
            exit_slip = exit_value * SLIPPAGE_RATE
            net = gross - exit_fee - exit_slip
            async with self.db.pool.acquire() as conn:
                async with conn.transaction():
                    updated = await conn.fetchval(
                        """
                        UPDATE t100_positions SET
                            status='closed',closed_at=$2,exit_price=$3,exit_reason=$4,
                            gross_pnl_usdt=$5,exit_fee_usdt=$6,exit_slippage_usdt=$7,
                            net_pnl_usdt=$8,current_price=$3,current_return_pct=$9,
                            mae_pct=$10,mfe_pct=$11,updated_at=now()
                        WHERE id=$1 AND status='open'
                        RETURNING id
                        """,
                        p["id"], eval_at, exit_px, decision.reason, gross,
                        exit_fee, exit_slip, net,
                        short_return_pct(entry, exit_px), mae, mfe,
                    )
                    if not updated:
                        continue
                    await conn.execute(
                        """
                        UPDATE t100_runtime
                        SET realized_equity_usdt=realized_equity_usdt+$1,updated_at=now()
                        WHERE singleton=true
                        """,
                        net,
                    )
                    await conn.execute(
                        """
                        INSERT INTO t100_events(event_type,symbol,position_id,payload)
                        VALUES('exit',$1,$2,$3::jsonb)
                        """,
                        p["symbol"], p["id"],
                        json.dumps(
                            {
                                "reason": decision.reason,
                                "exit_price": exit_px,
                                "gross_pnl": gross,
                                "net_pnl_before_funding": net,
                            }
                        ),
                    )
            await self.notifier.send(
                "T100_225 PAPER EXIT",
                f"{p['symbol']} • {decision.reason}",
                [
                    {"name": "Exit", "value": f"{exit_px:.10g}", "inline": True},
                    {"name": "Net P&L", "value": f"{net:+.2f} USDT", "inline": True},
                ],
            )

    async def _admit_pending_stage2(self, eval_at: datetime) -> None:
        rows = await self.db.pool.fetch(
            """
            SELECT * FROM t100_p2_signals
            WHERE eligible_stage2=true AND trade_decision IS NULL AND p2_at<=$1
            ORDER BY p2_at,id
            """,
            eval_at,
        )
        for signal_row in rows:
            signal_row = dict(signal_row)
            positions = await self._open_positions()
            symbols = {str(p["symbol"]) for p in positions}
            slots = {int(p["slot_no"]) for p in positions}
            if signal_row["symbol"] in symbols:
                decision, reason = "ignored_duplicate_symbol", "one position per symbol"
            elif len(positions) >= MAX_OPEN_POSITIONS:
                decision, reason = "ignored_capacity", "all four T100_225 slots occupied"
            elif signal_row["t100_tier"] not in {"LOW_100", HIGH_TIER, LEGACY_HIGH_TIER}:
                decision, reason = "ignored_invalid", "eligible Stage2 missing frozen ATR tier"
            else:
                equity = await self._equity(positions)
                fraction = notional_fraction(str(signal_row["t100_tier"]))
                notional = equity * fraction
                if equity <= 0 or notional <= 0:
                    decision, reason = "ignored_no_equity", "paper equity is non-positive"
                else:
                    slot = next(i for i in range(1, MAX_OPEN_POSITIONS + 1) if i not in slots)
                    entry = float(signal_row["entry_price"])
                    entry_fee = notional * FEE_RATE
                    entry_slip = notional * SLIPPAGE_RATE
                    quantity = notional / entry
                    async with self.db.pool.acquire() as conn:
                        async with conn.transaction():
                            claimed = await conn.fetchval(
                                """
                                UPDATE t100_p2_signals
                                SET trade_decision='accepted',
                                    trade_decision_reason='T100_225 Stage2 admitted'
                                WHERE id=$1 AND trade_decision IS NULL
                                RETURNING id
                                """,
                                signal_row["id"],
                            )
                            if not claimed:
                                continue
                            pos_id = await conn.fetchval(
                                """
                                INSERT INTO t100_positions(
                                    signal_id,symbol,slot_no,tier,opened_at,entry_price,
                                    notional_usdt,quantity,entry_fee_usdt,entry_slippage_usdt,
                                    trail_activation_pct,trail_gap_pct,
                                    current_price,current_return_pct,metadata
                                ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$6,0,$13::jsonb)
                                RETURNING id
                                """,
                                signal_row["id"], signal_row["symbol"], slot,
                                signal_row["t100_tier"], signal_row["p2_at"], entry,
                                notional, quantity, entry_fee, entry_slip,
                                TRAIL_ACTIVATION_PCT, TRAIL_GAP_PCT,
                                json.dumps(
                                    {
                                        "strategy_id": STRATEGY_ID,
                                        "trail_activation_pct": TRAIL_ACTIVATION_PCT,
                                        "trail_gap_pct": TRAIL_GAP_PCT,
                                        "stage1_return_24h": signal_row["stage1_return_24h"],
                                        "stage1_atr7_pct": signal_row["stage1_atr7_pct"],
                                    },
                                    default=str,
                                ),
                            )
                            await conn.execute(
                                """
                                UPDATE t100_runtime
                                SET realized_equity_usdt=realized_equity_usdt-$1,updated_at=now()
                                WHERE singleton=true
                                """,
                                entry_fee + entry_slip,
                            )
                            await conn.execute(
                                """
                                INSERT INTO t100_events(event_type,symbol,signal_id,position_id,payload)
                                VALUES('entry',$1,$2,$3,$4::jsonb)
                                """,
                                signal_row["symbol"], signal_row["id"], pos_id,
                                json.dumps(
                                    {
                                        "tier": signal_row["t100_tier"],
                                        "slot": slot,
                                        "notional": notional,
                                        "entry": entry,
                                        "trail_activation_pct": TRAIL_ACTIVATION_PCT,
                                        "trail_gap_pct": TRAIL_GAP_PCT,
                                    }
                                ),
                            )
                    await self.notifier.send(
                        "T100_225 PAPER ENTRY",
                        f"{signal_row['symbol']} • {signal_row['t100_tier']} • slot {slot}/4",
                        [
                            {"name": "Entry", "value": f"{entry:.10g}", "inline": True},
                            {"name": "Notional", "value": f"{notional:.2f} USDT", "inline": True},
                            {
                                "name": "Stage1 gate",
                                "value": (
                                    f"r24={float(signal_row['stage1_return_24h'])*100:.2f}% • "
                                    f"ATR7={float(signal_row['stage1_atr7_pct']):.2f}%"
                                ),
                                "inline": False,
                            },
                        ],
                    )
                    continue
            await self.db.pool.execute(
                """
                UPDATE t100_p2_signals
                SET trade_decision=$2,trade_decision_reason=$3
                WHERE id=$1 AND trade_decision IS NULL
                """,
                signal_row["id"], decision, reason,
            )

    async def process_eval(self, eval_at: datetime) -> None:
        # First settle/examine the bar that just completed for positions already open.
        await self._process_position_bar(eval_at)

        rows = await self._build_eval_rows(eval_at)
        for row in rows:
            await self._process_symbol_row(row)

        # Then admit Stage2 signals created at this completion timestamp.
        await self._admit_pending_stage2(eval_at)
        await self.db.pool.execute(
            """
            UPDATE t100_runtime SET last_eval_at=$1,updated_at=now() WHERE singleton=true
            """,
            eval_at,
        )
        positions = await self._open_positions()
        equity = await self._equity(positions)
        await self.db.heartbeat(
            "mexc-t100-200-paper",
            {
                "strategy_id": STRATEGY_ID,
                "run_id": PAPER_RUN_ID,
                "mode": "paper",
                "last_eval_at": eval_at.isoformat(),
                "open_positions": len(positions),
                "equity_usdt": round(equity, 4),
            },
        )
        LOGGER.info(
            "T100 eval complete %s rows=%d open=%d equity=%.2f",
            eval_at.isoformat(), len(rows), len(positions), equity,
        )

    async def cycle(self) -> None:
        latest_eval = _floor_30m(datetime.now(UTC))
        runtime = await self._runtime()
        last_eval = runtime.get("last_eval_at")

        # No new native Min30 close: do not hammer the full universe. Keep only
        # idempotent pending-admission recovery and heartbeat alive.
        if last_eval is not None and last_eval >= latest_eval:
            await self._admit_pending_stage2(latest_eval)
            positions = await self._open_positions()
            await self.db.heartbeat(
                "mexc-t100-200-paper",
                {
                    "strategy_id": STRATEGY_ID,
                    "run_id": PAPER_RUN_ID,
                    "mode": "paper",
                    "last_eval_at": last_eval.isoformat(),
                    "open_positions": len(positions),
                    "equity_usdt": round(await self._equity(positions), 4),
                },
            )
            return

        await self.refresh_contracts()
        await self.sync_market_history()
        if last_eval is None:
            evals = [latest_eval]
        else:
            next_eval = last_eval + M30
            evals = []
            while next_eval <= latest_eval:
                evals.append(next_eval)
                next_eval += M30
            # Bound restart catch-up. Candle sync has only a 10d normal bootstrap;
            # deeper outages fail closed instead of inventing incomplete lifecycle history.
            if len(evals) > 480:
                raise RuntimeError("T100 catch-up exceeds 10 days; manual reconstruction required")
        for eval_at in evals:
            await self.process_eval(eval_at)

    async def run(self) -> None:
        await self.initialize()
        await self.notifier.send(
            "T100_225 PAPER STARTED",
            "Frozen A14/G0.5 Stage2 P15_A4 strategy with T100_225 sizing is active in paper/shadow mode.",
            [
                {"name": "Strategy", "value": STRATEGY_ID, "inline": False},
                {"name": "Slots", "value": "4", "inline": True},
                {"name": "Sizing", "value": "LOW 25% / HIGH 56.25% equity", "inline": True},
                {"name": "Exit", "value": "SL75 → trail +14%, gap 0.5pp", "inline": False},
            ],
        )
        try:
            while not self.stop_event.is_set():
                started = asyncio.get_running_loop().time()
                try:
                    await self.cycle()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception("T100 cycle failed")
                    await self.notifier.send(
                        "T100_225 PAPER ERROR",
                        "Cycle failed; strategy remains fail-closed until the next successful cycle.",
                    )
                elapsed = asyncio.get_running_loop().time() - started
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(),
                        timeout=max(1.0, self.poll_seconds - elapsed),
                    )
                except TimeoutError:
                    pass
        finally:
            await self.close()


async def main() -> None:
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    worker = T100Worker()
    loop = asyncio.get_running_loop()
    for system_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(system_signal, worker.stop_event.set)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())

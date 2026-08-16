from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Awaitable, Callable

from app.config import Settings
from app.db import Database
from app.indicators import (
    atr,
    close_location,
    ema,
    pct_return,
    percentile_rank,
    upper_wick_ratio,
    zscore_last,
)
from app.mexc import MexcClient, is_crypto_usdt_contract
from app.models import PumpEpisode, RunSignal, Ticker
from app.notifier import DiscordNotifier
from app.trader_db import TraderRepository
from app.trader_notifier import TraderNotifier
from app.performance import build_performance_summary, short_return, should_send_daily_report
from app.signals import (
    ExhaustionFeatures,
    ExhaustionThresholds,
    MarketStateThresholds,
    RunFeatures,
    RunThresholds,
    classify_execution_risk,
    classify_market_state,
    evaluate_failed_retest,
    score_exhaustion,
    score_run,
)

LOGGER = logging.getLogger(__name__)


class ScannerWorker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.database_url)
        self.mexc = MexcClient(
            settings.mexc_base_url,
            spot_base_url=settings.mexc_spot_base_url,
            request_rate_per_second=settings.request_rate_per_second,
            request_concurrency=settings.request_concurrency,
        )
        self.notifier = DiscordNotifier(
            settings.discord_webhook_url,
            settings.discord_signal_levels,
            performance_webhook_url=settings.discord_performance_webhook_url,
        )
        self.trader_watchdog_notifier = TraderNotifier(settings.discord_trader_events_webhook_url)
        self.trader_repo = TraderRepository(self.db)
        self._trader_watchdog_alerted = False
        self.stop_event = asyncio.Event()
        self.contracts: set[str] = set()
        self.latest_tickers: dict[str, Ticker] = {}
        self.wide_return_72h: dict[str, float] = {}
        self.last_ticker_store = 0.0
        self.thresholds = RunThresholds(
            min_amount_24h=settings.min_amount_24h,
            max_spread_pct=settings.max_spread_pct,
        )
        self.exhaustion_thresholds = ExhaustionThresholds()
        self.state_thresholds = MarketStateThresholds(
            min_run_score=settings.state_min_run_score,
            run_watch_min_24h=settings.run_watch_min_24h,
            run_watch_min_72h=settings.run_watch_min_72h,
            exhaustion_watch_min_72h=settings.exhaustion_watch_min_72h,
            exhaustion_watch_min_24h=settings.exhaustion_watch_min_24h,
            exhaustion_watch_max_24h=settings.exhaustion_watch_max_24h,
            active_exhaustion_min_score=settings.active_exhaustion_min_score,
        )

    async def run(self) -> None:
        await self.db.connect()
        await self.db.migrate()
        LOGGER.info("Database connected and migrations applied")

        await self.refresh_contracts()
        await self.collect_tickers(force_store=True)
        # Bootstrap a lightweight 4h scan across the entire crypto universe so
        # low-liquidity runners that are already cooling are not invisible.
        await self.collect_wide_scan()

        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(
                    self._periodic(
                        "contracts",
                        self.settings.contract_refresh_seconds,
                        self.refresh_contracts,
                    )
                )
                group.create_task(
                    self._periodic(
                        "tickers",
                        self.settings.ticker_poll_seconds,
                        self.collect_tickers,
                    )
                )
                group.create_task(
                    self._periodic(
                        "wide_scan",
                        self.settings.wide_scan_seconds,
                        self.collect_wide_scan,
                    )
                )
                group.create_task(
                    self._periodic(
                        "candles",
                        self.settings.candle_poll_seconds,
                        self.collect_candles,
                    )
                )
                group.create_task(
                    self._periodic(
                        "funding",
                        self.settings.funding_refresh_seconds,
                        self.collect_funding,
                    )
                )
                group.create_task(
                    self._periodic(
                        "signals",
                        self.settings.signal_poll_seconds,
                        self.evaluate_signals,
                    )
                )
                group.create_task(
                    self._periodic(
                        "performance",
                        self.settings.performance_poll_seconds,
                        self.track_performance,
                    )
                )
                group.create_task(
                    self._periodic(
                        "performance_report",
                        self.settings.performance_report_check_seconds,
                        self.check_daily_performance_report,
                    )
                )
                group.create_task(self._periodic("heartbeat", 60, self.write_heartbeat))
                group.create_task(self._periodic("trader_watchdog", 60, self.check_trader_watchdog))
                await self.stop_event.wait()
                raise StopWorker()
        except* StopWorker:
            LOGGER.info("Shutdown requested")
        finally:
            await self.close()

    async def close(self) -> None:
        await self.notifier.close()
        await self.trader_watchdog_notifier.close()
        await self.mexc.close()
        await self.db.close()

    async def _periodic(
        self,
        name: str,
        seconds: int,
        function: Callable[..., Awaitable[None]],
    ) -> None:
        stagger = {
            "tickers": 0,
            "heartbeat": 2,
            "wide_scan": 3,
            "candles": 5,
            "signals": 10,
            "funding": 15,
            "contracts": 20,
            "performance": 25,
            "performance_report": 35,
            "trader_watchdog": 45,
        }.get(name, 0)
        if stagger:
            await asyncio.sleep(stagger)
        while not self.stop_event.is_set():
            started = asyncio.get_running_loop().time()
            try:
                await function()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("%s loop failed", name)
            elapsed = asyncio.get_running_loop().time() - started
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=max(1.0, seconds - elapsed)
                )
            except TimeoutError:
                pass

    async def refresh_contracts(self) -> None:
        rows, spot_usdt_assets = await asyncio.gather(
            self.mexc.get_contracts(),
            self.mexc.get_spot_usdt_assets(),
        )
        await self.db.upsert_contracts(rows)

        active_usdt = [
            row
            for row in rows
            if str(row.get("quoteCoin") or "").upper() == "USDT"
            and str(row.get("settleCoin") or "").upper() == "USDT"
            and int(row.get("state") or 0) == 0
            and not bool(row.get("isHidden", False))
        ]
        crypto_rows = [
            row
            for row in active_usdt
            if is_crypto_usdt_contract(
                row,
                spot_usdt_assets,
                require_spot_pair=self.settings.require_mexc_spot_pair,
            )
        ]
        self.contracts = {str(row["symbol"]).upper() for row in crypto_rows}
        excluded = sorted(
            str(row.get("symbol") or "").upper()
            for row in active_usdt
            if str(row.get("symbol") or "").upper() not in self.contracts
        )
        LOGGER.info(
            "Refreshed contracts: total=%d active_usdt=%d crypto=%d excluded_non_crypto=%d examples=%s",
            len(rows),
            len(active_usdt),
            len(self.contracts),
            len(excluded),
            ",".join(excluded[:12]) or "none",
        )

    async def collect_tickers(self, force_store: bool = False) -> None:
        all_tickers = await self.mexc.get_tickers()
        tickers = [ticker for ticker in all_tickers if ticker.symbol in self.contracts]
        self.latest_tickers = {ticker.symbol: ticker for ticker in tickers}

        loop_time = asyncio.get_running_loop().time()
        should_store = (
            force_store
            or loop_time - self.last_ticker_store >= self.settings.ticker_store_seconds
        )
        stored = 0
        if should_store:
            bucket = self._time_bucket(
                datetime.now(UTC), self.settings.ticker_store_seconds
            )
            symbols = await self.discovery_symbols(include_benchmark=True)
            selected = [
                self.latest_tickers[symbol]
                for symbol in symbols
                if symbol in self.latest_tickers
            ]
            await self.db.insert_tickers(
                [replace(ticker, observed_at=bucket) for ticker in selected]
            )
            self.last_ticker_store = loop_time
            stored = len(selected)
        LOGGER.info(
            "Ticker refresh: received=%d crypto=%d stored=%d",
            len(all_tickers),
            len(tickers),
            stored,
        )

    def execution_quality_symbols(self, include_benchmark: bool = True) -> list[str]:
        eligible = [
            ticker
            for ticker in self.latest_tickers.values()
            if ticker.amount24 >= self.settings.min_amount_24h
            and ticker.spread_pct is not None
            and ticker.spread_pct <= self.settings.max_spread_pct
        ]
        eligible.sort(key=lambda item: item.amount24, reverse=True)
        symbols = [ticker.symbol for ticker in eligible]
        if (
            include_benchmark
            and "BTC_USDT" in self.latest_tickers
            and "BTC_USDT" not in symbols
        ):
            symbols.append("BTC_USDT")
        return symbols

    async def discovery_symbols(self, include_benchmark: bool = True) -> list[str]:
        """Symbols worth full candle analysis, independent of execution liquidity.

        Standard-liquidity contracts are always kept. Low-liquidity contracts are
        admitted when their 24h return is unusual, when they rank in the upper
        part of the MEXC crypto cross-section, when the hourly wide scan detects
        a large 72h run, or while they have a live pump episode.
        """
        if not self.latest_tickers:
            return []

        active_episodes = await self.db.active_episode_symbols()
        target_tracking = await self.db.unresolved_profit_target_symbols()
        returns = [ticker.rise_fall_rate for ticker in self.latest_tickers.values()]
        standard = set(self.execution_quality_symbols(include_benchmark=False))

        selected: list[tuple[int, float, float, str]] = []
        for ticker in self.latest_tickers.values():
            rank = percentile_rank(ticker.rise_fall_rate, returns)
            is_active = ticker.symbol in active_episodes
            is_target_tracking = ticker.symbol in target_tracking
            is_standard = ticker.symbol in standard
            is_mover = ticker.rise_fall_rate >= self.settings.discovery_min_return_24h
            is_relative_mover = (
                rank is not None
                and rank >= self.settings.discovery_min_cross_section_percentile
            )
            return_72h = self.wide_return_72h.get(ticker.symbol)
            is_72h_mover = (
                return_72h is not None
                and return_72h >= self.settings.wide_scan_min_return_72h
            )
            if not (is_active or is_target_tracking or is_standard or is_mover or is_relative_mover or is_72h_mover):
                continue
            selected.append(
                (
                    3 if is_target_tracking else (2 if is_active else (1 if is_72h_mover else 0)),
                    max(ticker.rise_fall_rate, return_72h or -999.0),
                    ticker.amount24,
                    ticker.symbol,
                )
            )

        selected.sort(reverse=True)
        symbols = [item[3] for item in selected[: self.settings.max_symbols]]

        for symbol in sorted(self.settings.diagnostic_symbols):
            ticker = self.latest_tickers.get(symbol)
            if ticker is None:
                continue
            rank = percentile_rank(ticker.rise_fall_rate, returns)
            LOGGER.info(
                "Discovery diagnostic %s: selected=%s 24h=%.2f%% 72h=%s percentile=%s standard=%s active=%s",
                symbol,
                symbol in symbols,
                ticker.rise_fall_rate * 100.0,
                (
                    f"{self.wide_return_72h[symbol] * 100.0:.2f}%"
                    if symbol in self.wide_return_72h
                    else "n/a"
                ),
                (f"{rank * 100.0:.1f}%" if rank is not None else "n/a"),
                symbol in standard,
                symbol in active_episodes,
            )

        # Active episodes and unresolved +20%-vs-liquidation races must never
        # disappear because of the discovery cap.
        for symbol in sorted(active_episodes | target_tracking):
            if symbol in self.latest_tickers and symbol not in symbols:
                symbols.append(symbol)

        if (
            include_benchmark
            and "BTC_USDT" in self.latest_tickers
            and "BTC_USDT" not in symbols
        ):
            symbols.append("BTC_USDT")
        return symbols


    async def collect_wide_scan(self) -> None:
        """Keep a lightweight 72h return for every crypto perpetual.

        v0.8 discovered low-liquidity contracts only from the current 24h ticker
        or an already-open episode. That misses coins whose pump happened earlier
        and whose current 24h return has already cooled. This scan downloads only
        recent 4h candles for the full crypto universe, once per hour by default.
        """
        if not self.contracts or not self.latest_tickers:
            return

        symbols = sorted(self.contracts)
        semaphore = asyncio.Semaphore(self.settings.request_concurrency)
        now = datetime.now(UTC)

        async def scan_symbol(symbol: str) -> tuple[str, float | None]:
            async with semaphore:
                await self._sync_interval(
                    symbol, "Hour4", bootstrap_days=4, overlap_hours=8
                )
                candles = await self.db.fetch_candles(symbol, "Hour4", 24)
                completed = [
                    candle
                    for candle in candles
                    if candle.open_time + timedelta(hours=4) <= now
                ]
                ticker = self.latest_tickers.get(symbol)
                if ticker is None or len(completed) < 19:
                    return symbol, None
                return symbol, pct_return(completed[-19].close, ticker.last_price)

        results = await asyncio.gather(
            *(scan_symbol(symbol) for symbol in symbols), return_exceptions=True
        )
        failures = 0
        returns: dict[str, float] = {}
        for symbol, result in zip(symbols, results, strict=True):
            if isinstance(result, Exception):
                failures += 1
                if symbol in self.settings.diagnostic_symbols:
                    LOGGER.warning("Wide scan failed for %s: %s", symbol, result)
                continue
            _, value = result
            if value is not None:
                returns[symbol] = value

        self.wide_return_72h = returns
        movers = {
            symbol: value
            for symbol, value in returns.items()
            if value >= self.settings.wide_scan_min_return_72h
        }
        LOGGER.info(
            "Wide 72h scan complete: symbols=%d returns=%d movers=%d failures=%d",
            len(symbols),
            len(returns),
            len(movers),
            failures,
        )
        for symbol in sorted(self.settings.diagnostic_symbols):
            ticker = self.latest_tickers.get(symbol)
            if ticker is None:
                LOGGER.info("Diagnostic %s: not present in crypto ticker universe", symbol)
                continue
            LOGGER.info(
                "Diagnostic %s: 24h=%.2f%% 72h=%s amount24=$%.0f spread=%s",
                symbol,
                ticker.rise_fall_rate * 100.0,
                (
                    f"{returns[symbol] * 100.0:.2f}%"
                    if symbol in returns
                    else "n/a"
                ),
                ticker.amount24,
                (f"{ticker.spread_pct:.3f}%" if ticker.spread_pct is not None else "n/a"),
            )

    async def collect_candles(self) -> None:
        symbols = await self.discovery_symbols(include_benchmark=True)
        if not symbols:
            LOGGER.info("No discovery symbols available for candle collection")
            return
        semaphore = asyncio.Semaphore(self.settings.request_concurrency)

        async def sync_symbol(symbol: str) -> None:
            async with semaphore:
                await self._sync_interval(
                    symbol, "Min15", bootstrap_days=14, overlap_hours=2
                )
                await self._sync_interval(
                    symbol, "Hour4", bootstrap_days=120, overlap_hours=12
                )

        results = await asyncio.gather(
            *(sync_symbol(symbol) for symbol in symbols), return_exceptions=True
        )
        failures = sum(isinstance(result, Exception) for result in results)
        for symbol, result in zip(symbols, results, strict=True):
            if isinstance(result, Exception):
                LOGGER.warning("Candle sync failed for %s: %s", symbol, result)
        LOGGER.info(
            "Candle sync complete: symbols=%d failures=%d", len(symbols), failures
        )

    async def _sync_interval(
        self, symbol: str, interval: str, bootstrap_days: int, overlap_hours: int
    ) -> None:
        latest = await self.db.latest_candle_time(symbol, interval)
        start = (
            datetime.now(UTC) - timedelta(days=bootstrap_days)
            if latest is None
            else latest - timedelta(hours=overlap_hours)
        )
        candles = await self.mexc.get_klines(
            symbol, interval, int(start.timestamp())
        )
        await self.db.upsert_candles(candles)

    async def collect_funding(self) -> None:
        symbols = await self.discovery_symbols(include_benchmark=False)
        semaphore = asyncio.Semaphore(self.settings.request_concurrency)

        async def sync_symbol(symbol: str) -> None:
            async with semaphore:
                rows = await self.mexc.get_funding_history(symbol)
                await self.db.upsert_funding_history(symbol, rows)

        results = await asyncio.gather(
            *(sync_symbol(symbol) for symbol in symbols), return_exceptions=True
        )
        failures = sum(isinstance(result, Exception) for result in results)
        LOGGER.info(
            "Funding sync complete: symbols=%d failures=%d", len(symbols), failures
        )

    async def evaluate_signals(self) -> None:
        symbols = [
            symbol
            for symbol in await self.discovery_symbols(include_benchmark=False)
            if symbol not in self.settings.excluded_symbols
        ]
        btc = self.latest_tickers.get("BTC_USDT")
        if not symbols or btc is None:
            return

        universe_returns = [
            ticker.rise_fall_rate for ticker in self.latest_tickers.values()
        ]
        evaluated = 0
        standard_evaluated = 0
        high_risk_evaluated = 0
        extreme_risk_evaluated = 0
        run_watches = 0
        exhaustion_watches = 0
        breakdown_watches = 0
        breakdown_waiting = 0
        confirmed_shorts = 0
        rearmed_episodes = 0

        for symbol in symbols:
            ticker = self.latest_tickers[symbol]
            candles_15m, candles_4h = await asyncio.gather(
                self.db.fetch_candles(symbol, "Min15", 400),
                self.db.fetch_candles(symbol, "Hour4", 80),
            )
            now = datetime.now(UTC)
            completed_15m = [
                item
                for item in candles_15m
                if item.open_time + timedelta(minutes=15) <= now
            ]
            completed_4h = [
                item
                for item in candles_4h
                if item.open_time + timedelta(hours=4) <= now
            ]
            if len(completed_15m) < 289 or len(completed_4h) < 25:
                if symbol in self.settings.diagnostic_symbols:
                    LOGGER.info(
                        "Signal diagnostic %s: waiting_for_history 15m=%d/289 4h=%d/25",
                        symbol,
                        len(completed_15m),
                        len(completed_4h),
                    )
                continue

            evaluated += 1
            closes_15m = [item.close for item in completed_15m]
            highs_15m = [item.high for item in completed_15m]
            lows_15m = [item.low for item in completed_15m]
            volumes_15m = [item.volume for item in completed_15m]
            closes_4h = [item.close for item in completed_4h]
            highs_4h = [item.high for item in completed_4h]
            lows_4h = [item.low for item in completed_4h]

            return_72h = pct_return(closes_15m[-289], ticker.last_price)
            ema20 = ema(closes_4h, 20)
            atr14_4h = atr(highs_4h, lows_4h, closes_4h, 14)
            atr14_15m = atr(highs_15m, lows_15m, closes_15m, 14)
            distance_atr = (
                (ticker.last_price - ema20) / atr14_4h
                if ema20 is not None and atr14_4h is not None and atr14_4h > 0
                else None
            )
            premium_pct = (
                (ticker.fair_price / ticker.index_price - 1.0) * 100.0
                if ticker.fair_price is not None
                and ticker.index_price is not None
                and ticker.index_price > 0
                else None
            )
            volume_z = zscore_last(volumes_15m, 96)
            features = RunFeatures(
                return_24h=ticker.rise_fall_rate,
                return_72h=return_72h,
                btc_return_24h=btc.rise_fall_rate,
                residual_return_24h=ticker.rise_fall_rate - btc.rise_fall_rate,
                cross_section_percentile=percentile_rank(
                    ticker.rise_fall_rate, universe_returns
                ),
                volume_zscore_15m=volume_z,
                distance_above_ema20_atr_4h=distance_atr,
                amount_24h=ticker.amount24,
                spread_pct=ticker.spread_pct,
                funding_rate=ticker.funding_rate,
                fair_index_premium_pct=premium_pct,
                hold_vol=ticker.hold_vol,
            )
            run_score, run_reasons, scorable = score_run(features, self.thresholds)
            risk = classify_execution_risk(
                features,
                self.thresholds,
                high_risk_min_amount_24h=self.settings.high_risk_min_amount_24h,
                high_risk_max_spread_pct=self.settings.high_risk_max_spread_pct,
            )
            if risk.tier == "standard":
                standard_evaluated += 1
            elif risk.tier == "high_risk":
                high_risk_evaluated += 1
            else:
                extreme_risk_evaluated += 1

            latest = completed_15m[-1]
            previous = completed_15m[-2]
            ema9_15m = ema(closes_15m, 9)
            momentum_1h = pct_return(closes_15m[-5], closes_15m[-1])
            previous_momentum_1h = pct_return(closes_15m[-9], closes_15m[-5])
            prior_support = min(item.low for item in completed_15m[-5:-1])
            structural_break = latest.close < prior_support
            exhaustion = ExhaustionFeatures(
                upper_wick_ratio_15m=upper_wick_ratio(
                    latest.open, latest.high, latest.low, latest.close
                ),
                close_location_15m=close_location(
                    latest.high, latest.low, latest.close
                ),
                momentum_1h=momentum_1h,
                previous_momentum_1h=previous_momentum_1h,
                momentum_decelerating=(
                    momentum_1h is not None
                    and previous_momentum_1h is not None
                    and momentum_1h < previous_momentum_1h
                ),
                below_ema9_15m=ema9_15m is not None and latest.close < ema9_15m,
                lower_high_and_close=(
                    latest.high < previous.high and latest.close < previous.close
                ),
                structural_break_15m=structural_break,
                volume_zscore_15m=volume_z,
            )
            exhaustion_score, exhaustion_reasons = score_exhaustion(
                exhaustion, self.exhaustion_thresholds
            )
            state, state_reasons = classify_market_state(
                features,
                run_score,
                exhaustion,
                exhaustion_score,
                self.state_thresholds,
            )
            if symbol in self.settings.diagnostic_symbols:
                LOGGER.info(
                    "Signal diagnostic %s: run_score=%d/6 exhaustion_score=%d/7 state=%s structural_break=%s risk=%s 24h=%.2f%% 72h=%.2f%%",
                    symbol,
                    run_score,
                    exhaustion_score,
                    state or "none",
                    exhaustion.structural_break_15m,
                    risk.tier,
                    (features.return_24h or 0.0) * 100.0,
                    (features.return_72h or 0.0) * 100.0,
                )

            signaled_at = self._time_bucket(
                now, self.settings.signal_poll_seconds
            )
            base_features = features.as_dict()
            base_features.update(exhaustion.as_dict())
            base_features["run_score"] = run_score
            base_features["exhaustion_score"] = exhaustion_score
            base_features["atr_15m"] = atr14_15m
            base_features["risk_tier"] = risk.tier
            base_features["execution_eligible"] = risk.execution_eligible
            base_features["execution_risk_reasons"] = list(risk.reasons)
            base_features["execution_risk_warning"] = risk.warning

            episode = await self.db.get_active_episode(symbol)

            # Close very old episodes so a later, unrelated pump can be tracked.
            if episode is not None and (
                now - episode.started_at
                > timedelta(hours=self.settings.episode_max_age_hours)
            ):
                await self.db.close_episode(
                    episode.id,
                    closed_at=now,
                    reason="episode max age exceeded",
                )
                episode = None

            # A confirmed episode is locked. It may only re-arm after a later
            # completed candle establishes a materially higher high.
            if episode is not None and episode.confirmed_short_at is not None:
                post_confirm = [
                    candle
                    for candle in completed_15m
                    if candle.open_time > episode.confirmed_short_at
                ]
                new_high = max(
                    (candle.high for candle in post_confirm), default=0.0
                )
                can_rearm = (
                    scorable
                    and run_score >= self.settings.state_min_run_score
                    and state is not None
                    and new_high
                    >= episode.peak_price * (1.0 + self.settings.rearm_new_high_pct)
                )
                if can_rearm:
                    await self.db.close_episode(
                        episode.id,
                        closed_at=now,
                        reason=(
                            "rearmed after new high >= "
                            f"{self.settings.rearm_new_high_pct:.1%} above prior episode peak"
                        ),
                    )
                    episode = None
                    rearmed_episodes += 1
                else:
                    continue

            # An active breakdown must keep being evaluated even if run_score
            # later drops below the normal watch threshold.
            if episode is not None and episode.state == "breakdown_watch":
                if (
                    episode.breakdown_at is None
                    or episode.broken_level is None
                    or episode.breakdown_atr_15m is None
                ):
                    episode = await self.db.update_episode(
                        episode.id,
                        state=state or "exhaustion_watch",
                        run_score=run_score,
                        exhaustion_score=exhaustion_score,
                        clear_breakdown=True,
                    )
                else:
                    retest = evaluate_failed_retest(
                        completed_15m,
                        breakdown_at=episode.breakdown_at,
                        broken_level=episode.broken_level,
                        atr_15m=episode.breakdown_atr_15m,
                        tolerance_atr=self.settings.retest_tolerance_atr,
                        window_candles=self.settings.retest_window_candles,
                    )
                    if retest.confirmed:
                        confirm_features = dict(base_features)
                        confirm_features.update(
                            {
                                "episode_peak_price": episode.peak_price,
                                "broken_level": episode.broken_level,
                                "breakdown_at": episode.breakdown_at,
                                "retest_at": retest.retest_at,
                                "retest_high": retest.retest_high,
                                "retest_close": retest.retest_close,
                                "retest_tolerance_atr": self.settings.retest_tolerance_atr,
                                "retest_window_candles": self.settings.retest_window_candles,
                            }
                        )
                        episode = await self.db.update_episode(
                            episode.id,
                            state="confirmed_short",
                            retest_at=retest.retest_at,
                            confirmed_short_at=signaled_at,
                            run_score=run_score,
                            exhaustion_score=exhaustion_score,
                            metadata={"confirmation_reason": retest.reason},
                        )
                        signal_obj = RunSignal(
                            symbol=symbol,
                            signaled_at=signaled_at,
                            level="confirmed_short",
                            score=run_score + exhaustion_score,
                            features=confirm_features,
                            reasons=[
                                "prior pump episode entered breakdown watch",
                                retest.reason or "failed retest confirmed",
                                "one confirmed short allowed for this episode",
                            ],
                            episode_id=episode.id,
                        )
                        inserted = await self.db.insert_signal(signal_obj)
                        if retest.retest_close is not None and retest.retest_close > 0:
                            await self.db.create_shadow_trade(
                                episode_id=episode.id,
                                symbol=symbol,
                                confirmed_at=signaled_at,
                                entry_price=retest.retest_close,
                                risk_tier=risk.tier,
                            )
                        if inserted:
                            confirmed_shorts += 1
                            await self.notifier.send_signal(signal_obj)
                        continue

                    if retest.invalidated:
                        fallback_state = state or "exhaustion_watch"
                        old_state = episode.state
                        episode = await self.db.update_episode(
                            episode.id,
                            state=fallback_state,
                            run_score=run_score,
                            exhaustion_score=exhaustion_score,
                            metadata={"last_breakdown_result": retest.reason},
                            clear_breakdown=True,
                        )
                        if state is not None and old_state != fallback_state:
                            sent = await self._emit_watch_transition(
                                symbol=symbol,
                                state=fallback_state,
                                signaled_at=signaled_at,
                                run_score=run_score,
                                exhaustion_score=exhaustion_score,
                                features=base_features,
                                reasons=[
                                    retest.reason or "breakdown invalidated",
                                    *state_reasons,
                                    *run_reasons,
                                    *(
                                        exhaustion_reasons
                                        if fallback_state == "exhaustion_watch"
                                        else []
                                    ),
                                ],
                                episode=episode,
                            )
                            if sent == "run_watch":
                                run_watches += 1
                            elif sent == "exhaustion_watch":
                                exhaustion_watches += 1
                        continue

                    if retest.expired:
                        episode = await self.db.update_episode(
                            episode.id,
                            state=state or "exhaustion_watch",
                            run_score=run_score,
                            exhaustion_score=exhaustion_score,
                            metadata={"last_breakdown_result": retest.reason},
                            clear_breakdown=True,
                        )
                        continue

                    breakdown_waiting += 1
                    await self.db.update_episode(
                        episode.id,
                        run_score=run_score,
                        exhaustion_score=exhaustion_score,
                    )
                    continue

            # Discovery is intentionally independent of execution liquidity.
            if (
                not scorable
                or run_score < self.settings.state_min_run_score
                or state is None
            ):
                continue

            peak_candle = max(completed_15m[-289:], key=lambda item: item.high)
            if episode is None:
                episode = await self.db.create_episode(
                    symbol=symbol,
                    started_at=signaled_at,
                    state=state,
                    peak_price=peak_candle.high,
                    peak_at=peak_candle.open_time,
                    run_score=run_score,
                    exhaustion_score=exhaustion_score,
                    metadata={"detected_return_72h": return_72h},
                )
                sent = await self._emit_watch_transition(
                    symbol=symbol,
                    state=state,
                    signaled_at=signaled_at,
                    run_score=run_score,
                    exhaustion_score=exhaustion_score,
                    features=base_features,
                    reasons=state_reasons
                    + run_reasons
                    + (exhaustion_reasons if state == "exhaustion_watch" else []),
                    episode=episode,
                )
                if sent == "run_watch":
                    run_watches += 1
                elif sent == "exhaustion_watch":
                    exhaustion_watches += 1
            else:
                old_state = episode.state
                peak_price = episode.peak_price
                peak_at = episode.peak_at
                if peak_candle.high > peak_price:
                    peak_price = peak_candle.high
                    peak_at = peak_candle.open_time
                episode = await self.db.update_episode(
                    episode.id,
                    state=state,
                    peak_price=peak_price,
                    peak_at=peak_at,
                    run_score=run_score,
                    exhaustion_score=exhaustion_score,
                )
                if old_state != state:
                    sent = await self._emit_watch_transition(
                        symbol=symbol,
                        state=state,
                        signaled_at=signaled_at,
                        run_score=run_score,
                        exhaustion_score=exhaustion_score,
                        features=base_features,
                        reasons=state_reasons
                        + run_reasons
                        + (
                            exhaustion_reasons
                            if state == "exhaustion_watch"
                            else []
                        ),
                        episode=episode,
                    )
                    if sent == "run_watch":
                        run_watches += 1
                    elif sent == "exhaustion_watch":
                        exhaustion_watches += 1

            # Structural break no longer produces a short. It arms the retest
            # state and stores the exact support level and ATR from this candle.
            if (
                state == "exhaustion_watch"
                and structural_break
                and exhaustion_score >= self.settings.short_exhaustion_score
                and atr14_15m is not None
                and atr14_15m > 0
            ):
                episode = await self.db.update_episode(
                    episode.id,
                    state="breakdown_watch",
                    broken_level=prior_support,
                    breakdown_at=latest.open_time,
                    breakdown_atr_15m=atr14_15m,
                    run_score=run_score,
                    exhaustion_score=exhaustion_score,
                    metadata={"breakdown_close": latest.close},
                )
                breakdown_features = dict(base_features)
                breakdown_features.update(
                    {
                        "episode_peak_price": episode.peak_price,
                        "broken_level": prior_support,
                        "breakdown_at": latest.open_time,
                        "retest_tolerance_atr": self.settings.retest_tolerance_atr,
                        "retest_window_candles": self.settings.retest_window_candles,
                    }
                )
                breakdown_signal = RunSignal(
                    symbol=symbol,
                    signaled_at=signaled_at,
                    level="breakdown_watch",
                    score=run_score + exhaustion_score,
                    features=breakdown_features,
                    reasons=state_reasons
                    + run_reasons
                    + exhaustion_reasons
                    + [
                        "structural break recorded; waiting for failed retest",
                        (
                            f"retest must occur within {self.settings.retest_window_candles} "
                            "completed 15m candles"
                        ),
                    ],
                    episode_id=episode.id,
                )
                if await self.db.insert_signal(breakdown_signal):
                    breakdown_watches += 1
                    await self.notifier.send_signal(breakdown_signal)

        LOGGER.info(
            "Signal evaluation: evaluated=%d standard=%d high_risk=%d extreme_risk=%d "
            "run_watches=%d exhaustion_watches=%d breakdown_watches=%d "
            "breakdown_waiting=%d confirmed_shorts=%d rearmed=%d",
            evaluated,
            standard_evaluated,
            high_risk_evaluated,
            extreme_risk_evaluated,
            run_watches,
            exhaustion_watches,
            breakdown_watches,
            breakdown_waiting,
            confirmed_shorts,
            rearmed_episodes,
        )

    async def _emit_watch_transition(
        self,
        *,
        symbol: str,
        state: str,
        signaled_at: datetime,
        run_score: int,
        exhaustion_score: int,
        features: dict[str, object],
        reasons: list[str],
        episode: PumpEpisode,
    ) -> str | None:
        cooldown = (
            self.settings.exhaustion_watch_alert_cooldown_minutes
            if state == "exhaustion_watch"
            else self.settings.run_watch_alert_cooldown_minutes
        )
        already_alerted = await self.db.recently_alerted(
            symbol, cooldown, level=state
        )
        state_features = dict(features)
        state_features["episode_peak_price"] = episode.peak_price
        signal_obj = RunSignal(
            symbol=symbol,
            signaled_at=signaled_at,
            level=state,
            score=run_score,
            features=state_features,
            reasons=reasons,
            episode_id=episode.id,
        )
        inserted = await self.db.insert_signal(signal_obj)
        if (
            inserted
            and not already_alerted
            and self.settings.watch_alerts_enabled
        ):
            await self.notifier.send_signal(signal_obj)
            return state
        return None

    async def track_performance(self) -> None:
        now = datetime.now(UTC)
        trades = await self.db.fetch_shadow_trades()
        updated = 0
        completed = 0
        failed = 0

        for trade in trades:
            try:
                fixed_complete = trade.get("return_168h_pct") is not None
                target_race_complete = (
                    trade.get("target_20_at") is not None
                    or trade.get("cross_400_breach_at") is not None
                )
                # Fixed-horizon analytics end at 7d, but the +20%-vs-liquidation
                # race is horizon-independent. Keep unresolved races alive until
                # +20% is observed or the +400% cross-buffer proxy is breached.
                if fixed_complete and target_race_complete:
                    completed += 1
                    continue

                episode_id = int(trade["episode_id"])
                symbol = str(trade["symbol"])
                confirmed_at = trade["confirmed_at"]
                entry_price = float(trade["entry_price"])
                horizon_end = confirmed_at + timedelta(hours=168)
                fixed_observation_end = min(now, horizon_end)

                current_price = None
                current_return = None
                ticker = self.latest_tickers.get(symbol)
                if ticker is not None:
                    current_price = ticker.last_price
                    current_return = short_return(entry_price, current_price)

                mfe = None
                mae = None
                if not fixed_complete:
                    # MFE/MAE remain strictly 7-day statistics. The independent
                    # +20% target race may continue beyond this observation window.
                    min_low, max_high = await self.db.candle_excursions(
                        symbol, confirmed_at, fixed_observation_end
                    )
                    mfe = (
                        max(0.0, short_return(entry_price, min_low))
                        if min_low is not None and min_low > 0
                        else None
                    )
                    mae = (
                        min(0.0, short_return(entry_price, max_high))
                        if max_high is not None and max_high > 0
                        else None
                    )

                horizons = (1, 4, 12, 24, 48, 72, 168)
                horizon_values: dict[int, float | None] = {h: None for h in horizons}
                horizon_times: dict[int, datetime | None] = {h: None for h in horizons}
                existing = {
                    1: trade["return_1h_pct"],
                    4: trade["return_4h_pct"],
                    12: trade["return_12h_pct"],
                    24: trade["return_24h_pct"],
                    48: trade.get("return_48h_pct"),
                    72: trade.get("return_72h_pct"),
                    168: trade.get("return_168h_pct"),
                }
                if not fixed_complete:
                    for hours in horizons:
                        if existing[hours] is not None:
                            continue
                        target = confirmed_at + timedelta(hours=hours)
                        if now < target:
                            continue
                        point = await self.db.candle_close_for_horizon(symbol, target)
                        if point is not None:
                            close_time, price = point
                            horizon_times[hours] = close_time
                            horizon_values[hours] = short_return(entry_price, price)

                path_events = {
                    "first_profit_at": None,
                    "target_20_at": None,
                    "isolated_100_breach_at": None,
                    "adverse_200_breach_at": None,
                    "adverse_300_breach_at": None,
                    "cross_400_breach_at": None,
                }
                if not fixed_complete or not target_race_complete:
                    # Fixed-horizon strategy analytics need the full adverse path through
                    # 7d even after +20% has already been touched. The target race may
                    # continue beyond 7d until +20% or the -400% proxy resolves it.
                    path_start = trade.get("last_observed_at") or confirmed_at
                    if path_start < confirmed_at:
                        path_start = confirmed_at
                    path_events = await self.db.trade_path_events(
                        symbol, path_start, now, entry_price
                    )

                await self.db.update_shadow_trade(
                    episode_id,
                    current_price=current_price,
                    current_return_pct=current_return,
                    observed_at=now,
                    mfe_pct=mfe,
                    mae_pct=mae,
                    return_1h_pct=horizon_values[1],
                    return_4h_pct=horizon_values[4],
                    return_12h_pct=horizon_values[12],
                    return_24h_pct=horizon_values[24],
                    return_48h_pct=horizon_values[48],
                    return_72h_pct=horizon_values[72],
                    return_168h_pct=horizon_values[168],
                    matured_at=(
                        horizon_times[24]
                        if trade.get("matured_at") is None
                        and horizon_values[24] is not None
                        else None
                    ),
                    matured_48h_at=(
                        horizon_times[48]
                        if trade.get("matured_48h_at") is None
                        and horizon_values[48] is not None
                        else None
                    ),
                    matured_72h_at=(
                        horizon_times[72]
                        if trade.get("matured_72h_at") is None
                        and horizon_values[72] is not None
                        else None
                    ),
                    matured_168h_at=(
                        horizon_times[168]
                        if trade.get("matured_168h_at") is None
                        and horizon_values[168] is not None
                        else None
                    ),
                    first_profit_at=path_events["first_profit_at"],
                    target_20_at=path_events["target_20_at"],
                    isolated_100_breach_at=path_events["isolated_100_breach_at"],
                    adverse_200_breach_at=path_events["adverse_200_breach_at"],
                    adverse_300_breach_at=path_events["adverse_300_breach_at"],
                    cross_400_breach_at=path_events["cross_400_breach_at"],
                )
                updated += 1
            except Exception:
                failed += 1
                LOGGER.exception(
                    "Performance tracking failed for episode=%s symbol=%s",
                    trade.get("episode_id"),
                    trade.get("symbol"),
                )

        LOGGER.info(
            "Performance tracker: tracked=%d complete_fixed_and_target=%d failed=%d total=%d",
            updated,
            completed,
            failed,
            len(trades),
        )

    async def check_daily_performance_report(self) -> None:
        """Check/report independently from trade tracking.

        A malformed trade or temporary candle-data problem must never prevent the
        daily Discord report from being evaluated.
        """
        now = datetime.now(UTC)
        try:
            await self._maybe_send_daily_performance(now)
        except Exception:
            LOGGER.exception("Daily performance report check failed")

    async def _maybe_send_daily_performance(self, now: datetime) -> None:
        last_date = await self.db.last_performance_report_date()
        due = should_send_daily_report(
            now,
            timezone_name=self.settings.performance_report_timezone,
            report_hour=self.settings.performance_report_hour,
            already_sent_date=last_date,
        )
        tz = ZoneInfo(self.settings.performance_report_timezone)
        local_now = now.astimezone(tz)
        if not due:
            # Keep normal logs quiet; heartbeat still proves the worker is alive.
            return

        LOGGER.info(
            "Daily performance report due: local_now=%s timezone=%s report_hour=%02d last_report_date=%s",
            local_now.isoformat(),
            self.settings.performance_report_timezone,
            self.settings.performance_report_hour,
            last_date,
        )

        rows = await self.db.performance_rows()
        report = build_performance_summary(
            rows,
            now_utc=now,
            timezone_name=self.settings.performance_report_timezone,
        )
        report_date = report.report_date

        sent = await self.notifier.send_performance_report(report)
        if not sent:
            LOGGER.warning("Daily performance report not sent; will retry on next performance cycle")
            return
        recorded = await self.db.record_performance_report(
            report_date=report_date,
            sent_at=now,
            timezone_name=self.settings.performance_report_timezone,
            payload=report.as_dict(),
        )
        if not recorded:
            return
        LOGGER.info(
            "Daily performance report: date=%s confirmed_today=%d matured24=%d matured48=%d matured72=%d matured7d=%d win24=%s win48=%s win72=%s win7d=%s",
            report_date,
            report.confirmed_today,
            report.horizon_24h.matured_total,
            report.horizon_48h.matured_total,
            report.horizon_72h.matured_total,
            report.horizon_168h.matured_total,
            report.horizon_24h.win_rate,
            report.horizon_48h.win_rate,
            report.horizon_72h.win_rate,
            report.horizon_168h.win_rate,
        )

    async def check_trader_watchdog(self) -> None:
        if not self.settings.discord_trader_events_webhook_url:
            return
        heartbeat = await self.db.worker_heartbeat("portfolio_short_trader")
        if not heartbeat:
            # Do not alert before this version of the trader has ever registered itself.
            return
        last_seen = heartbeat["last_seen_at"]
        now = datetime.now(UTC)
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=UTC)
        age = max(0.0, (now - last_seen).total_seconds())
        stale = age > self.settings.trader_watchdog_stale_seconds
        if stale and not self._trader_watchdog_alerted:
            self._trader_watchdog_alerted = True
            positions = await self.trader_repo.active_positions()
            runtime = await self.trader_repo.runtime()
            stats = await self.trader_repo.portfolio_stats()
            open_text = "\n".join(
                f"`#{p.slot_no or '-'} {p.symbol}` {p.risk_tier} last **{p.current_return_pct:+.1f}%** • peak {p.peak_profit_pct:+.1f}%"
                for p in positions[:10]
            ) or "No open positions in the trader database."
            await self.trader_watchdog_notifier.send(
                "🚨 TRADER HEARTBEAT LOST",
                "The scanner is healthy, but the trader process stopped updating its database heartbeat. "
                "This catches hard crashes that the trader process cannot report after it is already dead.",
                [
                    {"name": "Last trader heartbeat", "value": f"{last_seen.isoformat()} • **{age:.0f}s ago**", "inline": False},
                    {"name": "Last trader status", "value": str(heartbeat.get("status") or {})[:1024], "inline": False},
                    {"name": "Last known portfolio", "value": f"Paper realized **${float(runtime['paper_equity_usdt']):,.2f}** • closed {int(stats.get('closed_count') or 0)} • liquidations {int(stats.get('liquidation_count') or 0)}", "inline": False},
                    {"name": "Open positions (last DB state)", "value": open_text[:1024], "inline": False},
                ],
                color=0xE74C3C,
            )
        elif not stale and self._trader_watchdog_alerted:
            self._trader_watchdog_alerted = False
            await self.trader_watchdog_notifier.send(
                "🟢 TRADER HEARTBEAT RECOVERED",
                "The scanner can see fresh trader heartbeats again.",
                [{"name": "Heartbeat age", "value": f"{age:.0f}s", "inline": True}],
                color=0x2ECC71,
            )

    async def write_heartbeat(self) -> None:
        discovery = await self.discovery_symbols(include_benchmark=True)
        await self.db.heartbeat(
            "mexc-exhaustion-scanner",
            {
                "contracts": len(self.contracts),
                "latest_tickers": len(self.latest_tickers),
                "execution_quality_symbols": len(
                    self.execution_quality_symbols(include_benchmark=True)
                ),
                "discovery_symbols": len(discovery),
                "wide_scan_returns": len(self.wide_return_72h),
                "wide_scan_72h_movers": sum(
                    value >= self.settings.wide_scan_min_return_72h
                    for value in self.wide_return_72h.values()
                ),
                "execution_enabled": self.settings.execution_enabled,
                "strategy_version": "1.2.0",
            },
        )

    @staticmethod
    def _time_bucket(value: datetime, seconds: int) -> datetime:
        epoch = int(value.timestamp())
        bucket = epoch - epoch % seconds
        return datetime.fromtimestamp(bucket, UTC)


class StopWorker(Exception):
    pass


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )


async def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    worker = ScannerWorker(settings)
    loop = asyncio.get_running_loop()
    for system_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(system_signal, worker.stop_event.set)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())

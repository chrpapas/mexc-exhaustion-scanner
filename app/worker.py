from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
from app.signals import (
    ExhaustionFeatures,
    ExhaustionThresholds,
    MarketStateThresholds,
    RunFeatures,
    RunThresholds,
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
        self.notifier = DiscordNotifier(settings.discord_webhook_url)
        self.stop_event = asyncio.Event()
        self.contracts: set[str] = set()
        self.latest_tickers: dict[str, Ticker] = {}
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
                group.create_task(self._periodic("heartbeat", 60, self.write_heartbeat))
                await self.stop_event.wait()
                raise StopWorker()
        except* StopWorker:
            LOGGER.info("Shutdown requested")
        finally:
            await self.close()

    async def close(self) -> None:
        await self.notifier.close()
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
            "candles": 5,
            "signals": 10,
            "funding": 15,
            "contracts": 20,
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
            selected = [
                self.latest_tickers[symbol]
                for symbol in self.liquid_symbols(include_benchmark=True)
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

    def liquid_symbols(self, include_benchmark: bool = True) -> list[str]:
        eligible = [
            ticker
            for ticker in self.latest_tickers.values()
            if ticker.amount24 >= self.settings.min_amount_24h
            and ticker.spread_pct is not None
            and ticker.spread_pct <= self.settings.max_spread_pct
        ]
        eligible.sort(key=lambda item: item.amount24, reverse=True)
        symbols = [ticker.symbol for ticker in eligible[: self.settings.max_symbols]]
        if (
            include_benchmark
            and "BTC_USDT" in self.latest_tickers
            and "BTC_USDT" not in symbols
        ):
            symbols.append("BTC_USDT")
        return symbols

    async def collect_candles(self) -> None:
        symbols = self.liquid_symbols(include_benchmark=True)
        if not symbols:
            LOGGER.info("No liquid symbols available for candle collection")
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
        symbols = self.liquid_symbols(include_benchmark=False)
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
            for symbol in self.liquid_symbols(include_benchmark=False)
            if symbol not in self.settings.excluded_symbols
        ]
        btc = self.latest_tickers.get("BTC_USDT")
        if not symbols or btc is None:
            return

        universe_returns = [
            self.latest_tickers[symbol].rise_fall_rate for symbol in symbols
        ]
        evaluated = 0
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
            run_score, run_reasons, required_ok = score_run(features, self.thresholds)

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

            signaled_at = self._time_bucket(
                now, self.settings.signal_poll_seconds
            )
            base_features = features.as_dict()
            base_features.update(exhaustion.as_dict())
            base_features["run_score"] = run_score
            base_features["exhaustion_score"] = exhaustion_score
            base_features["atr_15m"] = atr14_15m

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
                    required_ok
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
                        if await self.db.insert_signal(signal_obj):
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

            # New watch states still require the normal run/liquidity gate.
            if (
                not required_ok
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
            "Signal evaluation: evaluated=%d run_watches=%d exhaustion_watches=%d "
            "breakdown_watches=%d breakdown_waiting=%d confirmed_shorts=%d rearmed=%d",
            evaluated,
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

    async def write_heartbeat(self) -> None:
        await self.db.heartbeat(
            "mexc-exhaustion-scanner",
            {
                "contracts": len(self.contracts),
                "latest_tickers": len(self.latest_tickers),
                "liquid_symbols": len(
                    self.liquid_symbols(include_benchmark=True)
                ),
                "execution_enabled": self.settings.execution_enabled,
                "strategy_version": "0.6",
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

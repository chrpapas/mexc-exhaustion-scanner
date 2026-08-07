from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable

from app.config import Settings
from app.db import Database
from app.indicators import atr, close_location, ema, pct_return, percentile_rank, upper_wick_ratio, zscore_last
from app.mexc import MexcClient, is_crypto_usdt_contract
from app.models import RunSignal, Ticker
from app.notifier import DiscordNotifier
from app.signals import (
    ExhaustionFeatures,
    ExhaustionThresholds,
    MarketStateThresholds,
    RunFeatures,
    RunThresholds,
    classify_market_state,
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
                group.create_task(self._periodic("contracts", self.settings.contract_refresh_seconds, self.refresh_contracts))
                group.create_task(self._periodic("tickers", self.settings.ticker_poll_seconds, self.collect_tickers))
                group.create_task(self._periodic("candles", self.settings.candle_poll_seconds, self.collect_candles))
                group.create_task(self._periodic("funding", self.settings.funding_refresh_seconds, self.collect_funding))
                group.create_task(self._periodic("signals", self.settings.signal_poll_seconds, self.evaluate_signals))
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
        stagger = {"tickers": 0, "heartbeat": 2, "candles": 5, "signals": 10, "funding": 15, "contracts": 20}.get(name, 0)
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
                await asyncio.wait_for(self.stop_event.wait(), timeout=max(1.0, seconds - elapsed))
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
        should_store = force_store or loop_time - self.last_ticker_store >= self.settings.ticker_store_seconds
        stored = 0
        if should_store:
            bucket = self._time_bucket(datetime.now(UTC), self.settings.ticker_store_seconds)
            selected = [self.latest_tickers[symbol] for symbol in self.liquid_symbols(include_benchmark=True)]
            await self.db.insert_tickers([replace(ticker, observed_at=bucket) for ticker in selected])
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
        if include_benchmark and "BTC_USDT" in self.latest_tickers and "BTC_USDT" not in symbols:
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
                await self._sync_interval(symbol, "Min15", bootstrap_days=14, overlap_hours=2)
                await self._sync_interval(symbol, "Hour4", bootstrap_days=120, overlap_hours=12)

        results = await asyncio.gather(*(sync_symbol(symbol) for symbol in symbols), return_exceptions=True)
        failures = sum(isinstance(result, Exception) for result in results)
        for symbol, result in zip(symbols, results, strict=True):
            if isinstance(result, Exception):
                LOGGER.warning("Candle sync failed for %s: %s", symbol, result)
        LOGGER.info("Candle sync complete: symbols=%d failures=%d", len(symbols), failures)

    async def _sync_interval(self, symbol: str, interval: str, bootstrap_days: int, overlap_hours: int) -> None:
        latest = await self.db.latest_candle_time(symbol, interval)
        start = (
            datetime.now(UTC) - timedelta(days=bootstrap_days)
            if latest is None
            else latest - timedelta(hours=overlap_hours)
        )
        candles = await self.mexc.get_klines(symbol, interval, int(start.timestamp()))
        await self.db.upsert_candles(candles)

    async def collect_funding(self) -> None:
        symbols = self.liquid_symbols(include_benchmark=False)
        semaphore = asyncio.Semaphore(self.settings.request_concurrency)

        async def sync_symbol(symbol: str) -> None:
            async with semaphore:
                rows = await self.mexc.get_funding_history(symbol)
                await self.db.upsert_funding_history(symbol, rows)

        results = await asyncio.gather(*(sync_symbol(symbol) for symbol in symbols), return_exceptions=True)
        failures = sum(isinstance(result, Exception) for result in results)
        LOGGER.info("Funding sync complete: symbols=%d failures=%d", len(symbols), failures)

    async def evaluate_signals(self) -> None:
        symbols = [
            symbol
            for symbol in self.liquid_symbols(include_benchmark=False)
            if symbol not in self.settings.excluded_symbols
        ]
        btc = self.latest_tickers.get("BTC_USDT")
        if not symbols or btc is None:
            return

        universe_returns = [self.latest_tickers[symbol].rise_fall_rate for symbol in symbols]
        evaluated = 0
        run_watches = 0
        exhaustion_watches = 0
        short_setups = 0

        for symbol in symbols:
            ticker = self.latest_tickers[symbol]
            candles_15m, candles_4h = await asyncio.gather(
                self.db.fetch_candles(symbol, "Min15", 400),
                self.db.fetch_candles(symbol, "Hour4", 80),
            )
            now = datetime.now(UTC)
            completed_15m = [item for item in candles_15m if item.open_time + timedelta(minutes=15) <= now]
            completed_4h = [item for item in candles_4h if item.open_time + timedelta(hours=4) <= now]
            if len(completed_15m) < 289 or len(completed_4h) < 25:
                continue

            evaluated += 1
            closes_15m = [item.close for item in completed_15m]
            volumes_15m = [item.volume for item in completed_15m]
            closes_4h = [item.close for item in completed_4h]
            highs_4h = [item.high for item in completed_4h]
            lows_4h = [item.low for item in completed_4h]

            return_72h = pct_return(closes_15m[-289], ticker.last_price)
            ema20 = ema(closes_4h, 20)
            atr14 = atr(highs_4h, lows_4h, closes_4h, 14)
            distance_atr = (
                (ticker.last_price - ema20) / atr14
                if ema20 is not None and atr14 is not None and atr14 > 0
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
                cross_section_percentile=percentile_rank(ticker.rise_fall_rate, universe_returns),
                volume_zscore_15m=volume_z,
                distance_above_ema20_atr_4h=distance_atr,
                amount_24h=ticker.amount24,
                spread_pct=ticker.spread_pct,
                funding_rate=ticker.funding_rate,
                fair_index_premium_pct=premium_pct,
                hold_vol=ticker.hold_vol,
            )
            run_score, run_reasons, required_ok = score_run(features, self.thresholds)
            if not required_ok or run_score < self.settings.state_min_run_score:
                continue

            latest = completed_15m[-1]
            previous = completed_15m[-2]
            ema9_15m = ema(closes_15m, 9)
            momentum_1h = pct_return(closes_15m[-5], closes_15m[-1])
            previous_momentum_1h = pct_return(closes_15m[-9], closes_15m[-5])
            prior_support = min(item.low for item in completed_15m[-5:-1])
            structural_break = latest.close < prior_support
            exhaustion = ExhaustionFeatures(
                upper_wick_ratio_15m=upper_wick_ratio(latest.open, latest.high, latest.low, latest.close),
                close_location_15m=close_location(latest.high, latest.low, latest.close),
                momentum_1h=momentum_1h,
                previous_momentum_1h=previous_momentum_1h,
                momentum_decelerating=(
                    momentum_1h is not None
                    and previous_momentum_1h is not None
                    and momentum_1h < previous_momentum_1h
                ),
                below_ema9_15m=ema9_15m is not None and latest.close < ema9_15m,
                lower_high_and_close=latest.high < previous.high and latest.close < previous.close,
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
            if state is None:
                continue

            signaled_at = self._time_bucket(now, self.settings.signal_poll_seconds)
            state_features = features.as_dict()
            state_features.update(exhaustion.as_dict())
            state_features["run_score"] = run_score
            state_features["exhaustion_score"] = exhaustion_score
            state_signal = RunSignal(
                symbol=symbol,
                signaled_at=signaled_at,
                level=state,
                score=run_score,
                features=state_features,
                reasons=state_reasons + run_reasons + (
                    exhaustion_reasons if state == "exhaustion_watch" else []
                ),
            )

            cooldown = (
                self.settings.exhaustion_watch_alert_cooldown_minutes
                if state == "exhaustion_watch"
                else self.settings.run_watch_alert_cooldown_minutes
            )
            already_alerted = await self.db.recently_alerted(symbol, cooldown, level=state)
            inserted = await self.db.insert_signal(state_signal)
            if inserted and not already_alerted and self.settings.watch_alerts_enabled:
                if state == "exhaustion_watch":
                    exhaustion_watches += 1
                else:
                    run_watches += 1
                await self.notifier.send_signal(state_signal)

            # A short setup is only allowed after the symbol has entered the
            # exhaustion state. A structural support break is mandatory.
            if state != "exhaustion_watch":
                continue
            if not structural_break or exhaustion_score < self.settings.short_exhaustion_score:
                continue

            short_features = dict(state_features)
            short_signal = RunSignal(
                symbol=symbol,
                signaled_at=signaled_at,
                level="short_setup",
                score=run_score + exhaustion_score,
                features=short_features,
                reasons=state_reasons + run_reasons + exhaustion_reasons,
            )
            if await self.db.recently_alerted(
                symbol, self.settings.short_alert_cooldown_minutes, level="short_setup"
            ):
                continue
            inserted_short = await self.db.insert_signal(short_signal)
            if inserted_short:
                short_setups += 1
                await self.notifier.send_signal(short_signal)

        LOGGER.info(
            "Signal evaluation: evaluated=%d run_watches=%d exhaustion_watches=%d short_setups=%d",
            evaluated,
            run_watches,
            exhaustion_watches,
            short_setups,
        )

    async def write_heartbeat(self) -> None:
        await self.db.heartbeat(
            "mexc-exhaustion-scanner",
            {
                "contracts": len(self.contracts),
                "latest_tickers": len(self.latest_tickers),
                "liquid_symbols": len(self.liquid_symbols(include_benchmark=True)),
                "execution_enabled": self.settings.execution_enabled,
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

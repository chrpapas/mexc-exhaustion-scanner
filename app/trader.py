from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from app.db import Database
from app.mexc_trade import MexcTradeClient, MexcTradeError
from app.trader_config import TraderSettings
from app.trader_db import TraderRepository
from app.trader_logic import (
    HTF_CROSS_SECTION_PERCENTILE_THRESHOLD,
    HTF_EMA_DISTANCE_ATR_THRESHOLD,
    HTF_PREVIOUS_MOMENTUM_1H_THRESHOLD,
    HTF_RETURN_24H_THRESHOLD,
    PCR_EMA_DISTANCE_ATR_THRESHOLD,
    PCR_RETURN_24H_THRESHOLD,
    htf_continuation_risk,
    htf_missing_features,
    htf_position_fraction,
    newly_breached_thresholds,
    parabolic_continuation_risk,
    pcr_position_fraction,
    protected_profit_floor_pct,
    short_price_for_return,
    short_return_pct,
    tier_strategy_exit_reason,
)
from app.trader_models import TradeSignal, TraderPosition
from app.trader_notifier import TraderNotifier

LOGGER = logging.getLogger(__name__)

GREEN = 0x2ECC71
YELLOW = 0xF1C40F
RED = 0xE74C3C
BLUE = 0x3498DB
PURPLE = 0x9B59B6
ORANGE = 0xE67E22


class PortfolioShortTrader:
    def __init__(self, settings: TraderSettings) -> None:
        self.settings = settings
        self.db = Database(settings.database_url)
        self.repo = TraderRepository(self.db)
        self.mexc = MexcTradeClient(
            settings.mexc_base_url,
            api_key=settings.mexc_api_key,
            api_secret=settings.mexc_api_secret,
            ws_url=settings.mexc_ws_url,
        )
        self.notifier = TraderNotifier(settings.trader_events_webhook_url)
        self._last_discord_heartbeat = 0.0
        self._last_error_alert: dict[str, float] = {}
        self._had_tick_error = False
        self._live_execution_halted = False
        self._last_snapshot_fields: list[dict[str, Any]] = []
        self._last_protection_check: dict[int, float] = {}
        self._active_run_id = "live"
        self._live_starting_available_usdt: float | None = None

    async def start(self) -> None:
        await self.db.connect()
        await self.db.migrate()
        await self.repo.initialize_runtime(
            starting_equity=self.settings.paper_starting_equity_usdt,
            process_existing=self.settings.process_existing_signals,
        )
        if self.settings.trading_mode == "paper":
            await self._ensure_paper_run()
        else:
            self._active_run_id = f"live_{self.settings.execution_strategy}"
            await self._live_preflight()
        LOGGER.info(
            "Trader started mode=%s run=%s risks=%s slots=%s slot_pct=%.4f max_exposure=%.2f%% strategy=%s",
            self.settings.trading_mode,
            self._active_run_id,
            ",".join(self.settings.allowed_risk_tiers),
            self.settings.max_open_positions,
            self.settings.slot_allocation_pct,
            self.settings.max_total_exposure_pct,
            self._strategy_label(),
        )
        # Discord is intentionally quiet: normal startup is logged/heartbeated in DB only.
        self._last_discord_heartbeat = time.monotonic()
        await self._write_db_heartbeat()

    async def _active_positions(self) -> list[TraderPosition]:
        # Keep paper and live books isolated even when they share the same PostgreSQL database.
        return [
            p for p in await self.repo.active_positions()
            if getattr(p, "mode", self.settings.trading_mode) == self.settings.trading_mode
        ]

    async def _ensure_paper_run(self) -> None:
        runtime = await self.repo.runtime()
        current_run = str(runtime.get("active_run_id") or "legacy_pre_v136")
        self._active_run_id = self.settings.paper_run_id
        if current_run == self.settings.paper_run_id:
            return

        # Reject an accidentally reused archived experiment ID before touching the active book.
        if await self.repo.run_record(self.settings.paper_run_id):
            raise RuntimeError(
                f"TRADER_PAPER_RUN_ID {self.settings.paper_run_id!r} was already used; "
                "choose a new unique run ID"
            )

        # Archive any old paper positions at the best price available. The new run then starts
        # from its configured clean equity, so these close values are historical bookkeeping only.
        old_positions = [p for p in await self.repo.active_positions() if p.mode == "paper"]
        for position in old_positions:
            try:
                price = await self.mexc.last_price(position.symbol)
            except Exception:
                LOGGER.warning(
                    "Could not fetch reset price for %s; using last database price %.10g",
                    position.symbol, position.current_price, exc_info=True,
                )
                price = position.current_price
            exit_fee = position.quantity_base * price * self.settings.paper_taker_fee_rate
            await self.repo.close_position(
                position,
                exit_price=price,
                status="closed",
                reason=f"strategy_run_reset_to_{self.settings.paper_run_id}",
                exit_fee_usdt=exit_fee,
            )
            await self.mexc.ticker_stream.remove(position.symbol)

        changed = await self.repo.activate_paper_run(
            run_id=self.settings.paper_run_id,
            starting_equity=self.settings.paper_starting_equity_usdt,
            process_existing=self.settings.process_existing_signals,
            strategy_name=self.settings.execution_strategy,
            metadata={
                "max_open_positions": self.settings.max_open_positions,
                "slot_allocation_pct": self.settings.slot_allocation_pct,
                "max_total_exposure_pct": self.settings.max_total_exposure_pct,
                "tp5_target_pct": self.settings.tp5_target_pct,
                "allowed_risk_tiers": list(self.settings.allowed_risk_tiers),
            },
        )
        if changed:
            LOGGER.warning(
                "Paper strategy run reset old_run=%s new_run=%s archived_open=%s starting_equity=%.2f strategy=%s",
                current_run, self.settings.paper_run_id, len(old_positions),
                self.settings.paper_starting_equity_usdt, self.settings.execution_strategy,
            )

    async def close(self) -> None:
        await asyncio.gather(
            self.mexc.close(), self.notifier.close(), self.db.close(), return_exceptions=True
        )

    async def run(self) -> None:
        started = False
        try:
            try:
                await self.start()
                started = True
            except Exception as exc:
                LOGGER.exception("Trader startup failed")
                await self._notify(
                    "🚨 TRADER STARTUP FAILED",
                    "The trader could not complete startup/preflight and will not trade until this is fixed.",
                    [
                        {"name": "Mode", "value": self.settings.trading_mode.upper(), "inline": True},
                        {"name": "Strategy", "value": self._strategy_label(), "inline": False},
                        {"name": "Error", "value": f"`{type(exc).__name__}: {str(exc)[:800]}`", "inline": False},
                    ],
                    color=RED,
                )
                raise

            while True:
                try:
                    await self.tick()
                    if self._had_tick_error:
                        self._had_tick_error = False
                        LOGGER.info("Trader recovered after previous server/API error")
                except Exception as exc:
                    LOGGER.exception("Trader tick failed")
                    self._had_tick_error = True
                    await self._alert_error("tick", exc)
                await asyncio.sleep(self.settings.poll_seconds)
        finally:
            if started:
                LOGGER.info("Trader process stopping")
            await self.close()

    async def tick(self) -> None:
        positions = await self._active_positions()
        live_rows: list[dict[str, Any]] | None = None
        if self.settings.trading_mode == "live":
            live_rows = await self.mexc.open_positions()
            await self._reconcile_live_positions(positions, live_rows)
            positions = await self._active_positions()

        # Fetch prices concurrently, then process state transitions serially to stay well within order mutation limits.
        if positions:
            results = await asyncio.gather(
                *(self.mexc.last_price(p.symbol) for p in positions), return_exceptions=True
            )
            for position, result in zip(positions, results, strict=True):
                if isinstance(result, Exception):
                    await self._alert_error(f"price:{position.symbol}", result)
                    continue
                try:
                    await self._monitor_position(position, float(result))
                except Exception as exc:
                    LOGGER.exception("Position monitor failed id=%s symbol=%s", position.id, position.symbol)
                    await self._alert_error(f"monitor:{position.symbol}", exc)

        await self._consume_new_signals()
        await self._maybe_heartbeat()
        await self._write_db_heartbeat()

    async def _live_preflight(self) -> None:
        await self.mexc.ping()
        asset = await self.mexc.usdt_asset()
        equity = float(asset.get("equity") or 0.0)
        if equity <= 0:
            raise RuntimeError("MEXC live preflight: USDT futures equity is zero")
        available = await self.mexc.usdt_available_balance()
        if available <= 0:
            raise RuntimeError("MEXC live preflight: available USDT futures balance is zero")
        self._live_starting_available_usdt = available
        mode = await self.mexc.position_mode()
        if mode is not None and mode != 1:
            raise RuntimeError(
                f"MEXC live preflight: hedge position mode (1) is required; account reports {mode}"
            )
        # Warm/cache contract metadata once to avoid the contract-detail endpoint rate limit during clustered signals.
        await self.mexc.refresh_contract_specs()
        exchange = await self.mexc.open_positions()
        managed = await self._active_positions()
        managed_ids = {p.mexc_position_id for p in managed if p.mode == "live" and p.mexc_position_id}
        extras = [r for r in exchange if int(r.get("positionId") or 0) not in managed_ids]
        if extras:
            raise RuntimeError(
                "MEXC live preflight found open futures positions not managed by this bot; "
                "refusing live startup"
            )

    async def _consume_new_signals(self) -> None:
        runtime = await self.repo.runtime()
        cursor = int(runtime["last_signal_id"])
        signals = await self.repo.next_confirmed_signals(cursor)
        for signal in signals:
            try:
                await self._handle_signal(signal)
            except Exception as exc:
                LOGGER.exception("Could not process signal id=%s symbol=%s", signal.id, signal.symbol)
                await self.repo.decision(signal.id, "error", str(exc))
                await self._alert_error(f"signal:{signal.symbol}", exc)
            finally:
                await self.repo.set_cursor(signal.id)

    async def _handle_signal(self, signal: TradeSignal) -> None:
        age = max(0.0, (datetime.now(UTC) - signal.signaled_at).total_seconds())
        if signal.risk_tier not in self.settings.allowed_risk_tiers:
            await self._ignore_signal(
                signal,
                "ignored_risk",
                f"risk tier {signal.risk_tier} not enabled",
                event_fields=[
                    {"name": "Allowed tiers", "value": ", ".join(self.settings.allowed_risk_tiers), "inline": False},
                ],
            )
            return
        if age > self.settings.max_signal_age_seconds:
            await self._ignore_signal(
                signal,
                "ignored_stale",
                f"signal age {age:.0f}s exceeds {self.settings.max_signal_age_seconds}s",
                event_fields=[
                    {"name": "Signal age", "value": f"{age / 60.0:.1f} min", "inline": True},
                    {"name": "Max age", "value": f"{self.settings.max_signal_age_seconds / 60.0:.1f} min", "inline": True},
                ],
            )
            return
        if self._live_execution_halted:
            await self._ignore_signal(
                signal,
                "ignored_invalid",
                "live execution halted by reconciliation safety check",
            )
            return

        active = await self._active_positions()
        if len(active) >= self.settings.max_open_positions:
            await self._ignore_signal(
                signal,
                "ignored_capacity",
                "all configured slots are occupied",
                active=active,
                event_fields=[
                    {"name": "Slots", "value": f"{len(active)}/{self.settings.max_open_positions}", "inline": True},
                ],
            )
            return
        if not self.settings.allow_same_symbol_parallel and any(p.symbol == signal.symbol for p in active):
            await self._ignore_signal(
                signal,
                "ignored_duplicate_symbol",
                f"{signal.symbol} already open",
                active=active,
            )
            return
        if self.settings.execution_strategy == "tier_v1" and signal.risk_tier == "STANDARD":
            standard_count = sum(p.risk_tier == "STANDARD" for p in active)
            if standard_count >= self.settings.max_standard_positions:
                await self._ignore_signal(
                    signal,
                    "ignored_capacity",
                    "STANDARD capacity reached; preserving the dedicated HIGH_RISK slot",
                    active=active,
                    event_fields=[
                        {
                            "name": "STANDARD capacity",
                            "value": f"{standard_count}/{self.settings.max_standard_positions}",
                            "inline": True,
                        },
                    ],
                )
                return
        if self.settings.execution_strategy == "tier_v1" and signal.risk_tier == "HIGH_RISK":
            high_count = sum(p.risk_tier == "HIGH_RISK" for p in active)
            if high_count >= self.settings.max_high_risk_positions:
                await self._ignore_signal(
                    signal,
                    "ignored_capacity",
                    "HIGH_RISK capacity reached; only one risky slot is allowed",
                    active=active,
                    event_fields=[
                        {
                            "name": "HIGH capacity",
                            "value": f"{high_count}/{self.settings.max_high_risk_positions}",
                            "inline": True,
                        },
                    ],
                )
                return

        equity = await self._account_equity(active)
        if equity <= 0:
            raise RuntimeError("account equity is not positive")
        total_notional = sum(p.notional_usdt for p in active)
        pcr_flagged = self.settings.uses_pcr_derisk and parabolic_continuation_risk(signal.features)
        htf_flagged = htf_continuation_risk(signal.features) if self.settings.uses_htf_derisk else None
        if self.settings.uses_htf_derisk and htf_flagged is None:
            missing = htf_missing_features(signal.features)
            await self._ignore_signal(
                signal,
                "ignored_missing_htf_data",
                "HTF V1 sizing is fail-closed because required signal-time data is missing: "
                + ", ".join(missing),
                active=active,
                event_fields=[
                    {"name": "Missing HTF inputs", "value": ", ".join(missing), "inline": False},
                ],
            )
            return
        if self.settings.uses_pcr_derisk:
            candidate_fraction = pcr_position_fraction(signal.features)
        elif self.settings.uses_htf_derisk:
            candidate_fraction = htf_position_fraction(signal.features)
            assert candidate_fraction is not None
        else:
            candidate_fraction = self.settings.slot_fraction
        desired = equity * candidate_fraction
        max_notional = equity * self.settings.max_total_exposure_fraction
        remaining = max_notional - total_notional
        current_exposure = total_notional / equity * 100.0 if equity > 0 else 0.0
        live_available: float | None = None
        if self.settings.trading_mode == "live":
            live_available = await self.mexc.usdt_available_balance()
            # Keep a small execution buffer for fees/price movement. At 1x this also prevents
            # the bot from requesting more margin than MEXC reports as immediately available.
            remaining = min(remaining, live_available * 0.98)
        minimum_slot = desired if self.settings.uses_generic_slots else desired * 0.5
        if remaining <= 0 or remaining + 1e-9 < minimum_slot:
            await self._ignore_signal(
                signal,
                "ignored_exposure",
                f"aggregate exposure cap reached ({current_exposure:.2f}% / {self.settings.max_total_exposure_pct:.2f}%)",
                active=active,
                event_fields=[
                    {"name": "Current exposure", "value": f"{current_exposure:.2f}%", "inline": True},
                    {"name": "Exposure cap", "value": f"{self.settings.max_total_exposure_pct:.2f}%", "inline": True},
                    {"name": "Requested slot", "value": f"${desired:,.2f} ({candidate_fraction * 100.0:.2f}%)", "inline": True},
                    {"name": "Remaining capacity", "value": f"${max(0.0, remaining):,.2f}", "inline": True},
                    *([{"name": "MEXC available USDT", "value": f"${live_available:,.2f}", "inline": True}] if live_available is not None else []),
                ],
            )
            return
        notional = desired if self.settings.uses_generic_slots else min(desired, remaining)
        slot_no = next(i for i in range(1, self.settings.max_open_positions + 1) if all(p.slot_no != i for p in active))

        position = (
            await self._open_paper(signal, slot_no, equity, notional)
            if self.settings.trading_mode == "paper"
            else await self._open_live(signal, slot_no, equity, notional)
        )
        LOGGER.info(
            "Signal accepted id=%s symbol=%s risk=%s pcr=%s htf=%s action=OPENED position_id=%s slot=%s notional=%.4f equity=%.4f",
            signal.id,
            signal.symbol,
            signal.risk_tier,
            pcr_flagged,
            htf_flagged,
            position.id,
            position.slot_no,
            position.notional_usdt,
            equity,
        )
        if self.settings.uses_catastrophic_stop:
            exit_text = (
                f"Full close at +{self.settings.tp5_target_pct:.0f}% favorable short return "
                f"or catastrophic stop at -{self.settings.catastrophic_stop_pct:.0f}%"
            )
        elif self.settings.uses_generic_slots:
            exit_text = f"Full close at +{self.settings.tp5_target_pct:.0f}% favorable short return"
        elif signal.risk_tier == "STANDARD":
            exit_text = f"Fixed {self.settings.standard_hold_days}d hold; +{self.settings.profit_target_pct:.0f}% is telemetry only"
        else:
            exit_text = f"Close at +{self.settings.profit_target_pct:.0f}% or after {self.settings.high_risk_timeout_days}d, whichever comes first"
        await self._notify(
            "🧾 PAPER SHORT OPENED" if position.mode == "paper" else "🔴 LIVE SHORT OPENED",
            f"**{position.symbol}** • {position.risk_tier} • slot {position.slot_no}",
            [
                {"name": "Entry", "value": f"{position.entry_price:.10g}", "inline": True},
                {"name": "Notional", "value": f"${position.notional_usdt:,.2f}", "inline": True},
                *([
                    {
                        "name": "PCR sizing",
                        "value": (
                            f"FLAGGED → {candidate_fraction * 100.0:.2f}% equity"
                            if pcr_flagged
                            else f"Unflagged → {candidate_fraction * 100.0:.2f}% equity"
                        ),
                        "inline": True,
                    }
                ] if self.settings.uses_pcr_derisk else []),
                *([
                    {
                        "name": "HTF V1 sizing",
                        "value": (
                            f"FLAGGED → {candidate_fraction * 100.0:.2f}% equity"
                            if htf_flagged
                            else f"Unflagged → {candidate_fraction * 100.0:.2f}% equity"
                        ),
                        "inline": True,
                    }
                ] if self.settings.uses_htf_derisk else []),
                {"name": "Exit plan", "value": exit_text, "inline": False},
                {
                    "name": "Risk control",
                    "value": (
                        f"1x cross • hard catastrophic stop -{self.settings.catastrophic_stop_pct:.0f}% • adverse telemetry remains active"
                        if self.settings.uses_catastrophic_stop
                        else "1x cross • no conventional tight stop • adverse breach telemetry remains active"
                    ),
                    "inline": False,
                },
            ],
            color=BLUE,
        )

    async def _ignore_signal(
        self,
        signal: TradeSignal,
        decision: str,
        reason: str,
        *,
        active: list[TraderPosition] | None = None,
        event_fields: list[dict[str, Any]] | None = None,
    ) -> None:
        """Persist, log and Discord-report every confirmed signal that is not traded."""
        await self.repo.decision(signal.id, decision, reason)
        active = active if active is not None else await self._active_positions()
        LOGGER.info(
            "Signal not traded id=%s symbol=%s risk=%s decision=%s reason=%s open_positions=%s/%s",
            signal.id,
            signal.symbol,
            signal.risk_tier,
            decision.upper(),
            reason,
            len(active),
            self.settings.max_open_positions,
        )
        # Skip decisions are intentionally Render-log/DB only. Discord is reserved for OPEN/CLOSE/ERROR.

    async def _open_paper(self, signal: TradeSignal, slot_no: int, equity: float, notional: float) -> TraderPosition:
        price = await self.mexc.last_price(signal.symbol)
        quantity = notional / price
        entry_fee = notional * self.settings.paper_taker_fee_rate
        runtime = await self.repo.runtime()
        await self.repo.set_paper_equity(max(0.0, float(runtime["paper_equity_usdt"]) - entry_fee))
        if self.settings.uses_generic_slots:
            exit_strategy = "tp5_sl75_full" if self.settings.uses_catastrophic_stop else "tp5_full"
            position_maturity = "profit_5"
        else:
            exit_strategy = "fixed_time_standard" if signal.risk_tier == "STANDARD" else "tp20_or_timeout"
            position_maturity = (
                f"{self.settings.standard_hold_days}d"
                if signal.risk_tier == "STANDARD"
                else f"{self.settings.high_risk_timeout_days}d"
            )
        return await self.repo.create_position(
            signal=signal,
            run_id=self._active_run_id,
            slot_no=slot_no,
            mode="paper",
            capital_strategy=self.settings.capital_strategy_label,
            exit_strategy=exit_strategy,
            position_maturity=position_maturity,
            entry_price=price,
            entry_equity_usdt=equity,
            notional_usdt=notional,
            quantity_base=quantity,
            liquidation_proxy_pct=400.0,
            entry_fee_usdt=entry_fee,
            metadata={
                "signal_entry_hint": signal.entry_hint,
                "position_fraction": notional / equity,
                "leverage": self.settings.leverage,
                "risk_tier": signal.risk_tier,
                "paper_taker_fee_rate": self.settings.paper_taker_fee_rate,
                "execution_strategy": self.settings.execution_strategy,
                "pcr_flagged": self.settings.uses_pcr_derisk and parabolic_continuation_risk(signal.features),
                "pcr_return_24h": signal.features.get("return_24h") if self.settings.uses_pcr_derisk else None,
                "pcr_ema_distance_atr_4h": signal.features.get("distance_above_ema20_atr_4h") if self.settings.uses_pcr_derisk else None,
                "pcr_return_24h_threshold": PCR_RETURN_24H_THRESHOLD if self.settings.uses_pcr_derisk else None,
                "pcr_ema_distance_atr_threshold": PCR_EMA_DISTANCE_ATR_THRESHOLD if self.settings.uses_pcr_derisk else None,
                "htf_v1_flagged": htf_continuation_risk(signal.features) if self.settings.uses_htf_derisk else None,
                "htf_v1_return_24h": signal.features.get("return_24h") if self.settings.uses_htf_derisk else None,
                "htf_v1_cross_section_percentile": signal.features.get("cross_section_percentile") if self.settings.uses_htf_derisk else None,
                "htf_v1_ema_distance_atr_4h": signal.features.get("distance_above_ema20_atr_4h") if self.settings.uses_htf_derisk else None,
                "htf_v1_previous_momentum_1h": signal.features.get("previous_momentum_1h") if self.settings.uses_htf_derisk else None,
                "htf_v1_return_24h_threshold": HTF_RETURN_24H_THRESHOLD if self.settings.uses_htf_derisk else None,
                "htf_v1_cross_section_threshold": HTF_CROSS_SECTION_PERCENTILE_THRESHOLD if self.settings.uses_htf_derisk else None,
                "htf_v1_ema_distance_atr_threshold": HTF_EMA_DISTANCE_ATR_THRESHOLD if self.settings.uses_htf_derisk else None,
                "htf_v1_previous_momentum_threshold": HTF_PREVIOUS_MOMENTUM_1H_THRESHOLD if self.settings.uses_htf_derisk else None,
                "tp_target_pct": self.settings.tp5_target_pct if self.settings.uses_generic_slots else self.settings.profit_target_pct,
                "catastrophic_stop_pct": self.settings.catastrophic_stop_pct if self.settings.uses_catastrophic_stop else None,
            },
        )

    async def _open_live(self, signal: TradeSignal, slot_no: int, equity: float, notional: float) -> TraderPosition:
        price = await self.mexc.last_price(signal.symbol)
        spec = await self.mexc.contract_spec(signal.symbol)
        if not spec.api_allowed:
            raise MexcTradeError(f"{signal.symbol} reports apiAllowed=false; MEXC API cannot trade it")
        if spec.position_open_type not in {self.settings.open_type, 3}:
            raise MexcTradeError(f"{signal.symbol} does not support requested margin mode")
        contracts = spec.contracts_for_notional(notional, price)
        order_id = await self.mexc.submit_market_short(
            symbol=signal.symbol,
            contracts=contracts,
            open_type=self.settings.open_type,
            reference_price=price,
            external_oid=f"exh-open-{signal.id}",
            leverage=self.settings.leverage,
        )
        live_position: dict[str, Any] | None = None
        for _ in range(16):
            await asyncio.sleep(0.5)
            rows = await self.mexc.open_positions(signal.symbol)
            live_position = next((r for r in rows if int(r.get("positionType") or 0) == 2), None)
            if live_position:
                break
        if not live_position:
            raise MexcTradeError(f"MEXC order {order_id} submitted but short position could not be confirmed")
        order = await self.mexc.order(order_id)
        entry_price = float(
            live_position.get("openAvgPriceFullyScale")
            or live_position.get("openAvgPrice")
            or order.get("dealAvgPrice")
            or price
        )
        hold_contracts = float(live_position.get("holdVol") or contracts)
        entry_fee = float(order.get("takerFee") or order.get("makerFee") or 0.0)
        if self.settings.uses_generic_slots:
            exit_strategy = "tp5_sl75_full" if self.settings.uses_catastrophic_stop else "tp5_full"
            position_maturity = "profit_5"
        else:
            exit_strategy = "fixed_time_standard" if signal.risk_tier == "STANDARD" else "tp20_or_timeout"
            position_maturity = (
                f"{self.settings.standard_hold_days}d"
                if signal.risk_tier == "STANDARD"
                else f"{self.settings.high_risk_timeout_days}d"
            )
        position = await self.repo.create_position(
            signal=signal,
            run_id=self._active_run_id,
            slot_no=slot_no,
            mode="live",
            capital_strategy=self.settings.capital_strategy_label,
            exit_strategy=exit_strategy,
            position_maturity=position_maturity,
            entry_price=entry_price,
            entry_equity_usdt=equity,
            notional_usdt=hold_contracts * spec.contract_size * entry_price,
            quantity_base=hold_contracts * spec.contract_size,
            liquidation_proxy_pct=400.0,
            entry_fee_usdt=entry_fee,
            mexc_position_id=int(live_position["positionId"]),
            mexc_open_order_id=order_id,
            metadata={
                "contracts": hold_contracts,
                "contract_size": spec.contract_size,
                "position_fraction": notional / equity,
                "leverage": self.settings.leverage,
                "risk_tier": signal.risk_tier,
                "mexc_liquidate_price": live_position.get("liquidatePrice"),
                "execution_strategy": self.settings.execution_strategy,
                "pcr_flagged": self.settings.uses_pcr_derisk and parabolic_continuation_risk(signal.features),
                "pcr_return_24h": signal.features.get("return_24h") if self.settings.uses_pcr_derisk else None,
                "pcr_ema_distance_atr_4h": signal.features.get("distance_above_ema20_atr_4h") if self.settings.uses_pcr_derisk else None,
                "pcr_return_24h_threshold": PCR_RETURN_24H_THRESHOLD if self.settings.uses_pcr_derisk else None,
                "pcr_ema_distance_atr_threshold": PCR_EMA_DISTANCE_ATR_THRESHOLD if self.settings.uses_pcr_derisk else None,
                "htf_v1_flagged": htf_continuation_risk(signal.features) if self.settings.uses_htf_derisk else None,
                "htf_v1_return_24h": signal.features.get("return_24h") if self.settings.uses_htf_derisk else None,
                "htf_v1_cross_section_percentile": signal.features.get("cross_section_percentile") if self.settings.uses_htf_derisk else None,
                "htf_v1_ema_distance_atr_4h": signal.features.get("distance_above_ema20_atr_4h") if self.settings.uses_htf_derisk else None,
                "htf_v1_previous_momentum_1h": signal.features.get("previous_momentum_1h") if self.settings.uses_htf_derisk else None,
                "htf_v1_return_24h_threshold": HTF_RETURN_24H_THRESHOLD if self.settings.uses_htf_derisk else None,
                "htf_v1_cross_section_threshold": HTF_CROSS_SECTION_PERCENTILE_THRESHOLD if self.settings.uses_htf_derisk else None,
                "htf_v1_ema_distance_atr_threshold": HTF_EMA_DISTANCE_ATR_THRESHOLD if self.settings.uses_htf_derisk else None,
                "htf_v1_previous_momentum_threshold": HTF_PREVIOUS_MOMENTUM_1H_THRESHOLD if self.settings.uses_htf_derisk else None,
                "tp_target_pct": self.settings.tp5_target_pct if self.settings.uses_generic_slots else self.settings.profit_target_pct,
                "catastrophic_stop_pct": self.settings.catastrophic_stop_pct if self.settings.uses_catastrophic_stop else None,
            },
        )
        if self.settings.uses_catastrophic_stop:
            stop_floor = -self.settings.catastrophic_stop_pct
            try:
                stop_order_id = await self._place_live_protection(position, stop_floor)
                await self.repo.mark_protection_armed(
                    position.id,
                    order_id=stop_order_id,
                    floor_pct=stop_floor,
                    price=entry_price,
                    return_pct=0.0,
                )
                position = (await self.repo.position(position.id)) or position
            except Exception as exc:
                LOGGER.exception(
                    "Catastrophic stop placement failed after opening live position id=%s symbol=%s",
                    position.id, position.symbol,
                )
                try:
                    await self._close(position, entry_price, "catastrophic_stop_setup_failed")
                finally:
                    raise RuntimeError(
                        f"live position {position.symbol} could not be protected by catastrophic stop"
                    ) from exc
        return position

    async def _monitor_position(self, position: TraderPosition, price: float) -> None:
        current_return = short_return_pct(position.entry_price, price)
        peak = max(position.peak_profit_pct, current_return)
        adverse = max(position.max_adverse_pct, max(0.0, -current_return))

        if position.mode == "live" and position.protection_armed_at is not None:
            await self._ensure_live_protection_exists(position)

        # Persist the current tick before sending any milestone notification so the
        # portfolio snapshot attached to Discord reflects the same price/P&L that
        # triggered the event rather than the previous tick.
        position = await self.repo.update_market(
            position.id,
            price=price,
            return_pct=current_return,
            peak_profit_pct=peak,
            max_adverse_pct=adverse,
            profit_floor_pct=position.profit_floor_pct,
        )

        already = {
            t for t, at in ((100, position.breach_100_at), (200, position.breach_200_at), (300, position.breach_300_at), (400, position.breach_400_at)) if at is not None
        }
        for threshold in newly_breached_thresholds(max_adverse_pct=adverse, already_breached=already):
            if await self.repo.mark_breach(position.id, threshold, price=price, return_pct=current_return):
                position = (await self.repo.position(position.id)) or position
                LOGGER.warning(
                    "Adverse level breached position_id=%s symbol=%s threshold=-%s%% current_return=%.4f max_adverse=%.4f",
                    position.id, position.symbol, threshold, current_return, adverse,
                )
                breach_titles = {
                    100: "⚠️ -100% ADVERSE BREACH",
                    200: "🟥 -200% ADVERSE BREACH",
                    300: "🟣 -300% ADVERSE BREACH",
                    400: "⚫ -400% ADVERSE BREACH",
                }
                breach_colors = {100: ORANGE, 200: RED, 300: PURPLE, 400: RED}
                await self._notify(
                    breach_titles[threshold],
                    f"**{position.symbol}** crossed the -{threshold}% adverse-return level. The position remains open unless another execution rule closes it.",
                    [
                        {"name": "Current return", "value": f"{current_return:+.2f}%", "inline": True},
                        {"name": "Max adverse", "value": f"-{adverse:.2f}%", "inline": True},
                        {"name": "Current price", "value": f"{price:.10g}", "inline": True},
                        {"name": "Entry", "value": f"{position.entry_price:.10g}", "inline": True},
                        {"name": "Exit target", "value": (f"+{float(position.metadata.get('tp_target_pct') or self.settings.tp5_target_pct):g}%" if position.exit_strategy == "tp5_full" else f"+{self.settings.profit_target_pct:.0f}%"), "inline": True},
                    ],
                    color=breach_colors[threshold],
                )

        if position.exit_strategy in {"tp5_full", "tp5_sl75_full"}:
            target_pct = float(position.metadata.get("tp_target_pct") or self.settings.tp5_target_pct)
            if position.exit_strategy == "tp5_sl75_full":
                stop_pct = float(position.metadata.get("catastrophic_stop_pct") or self.settings.catastrophic_stop_pct)
                if current_return <= -stop_pct:
                    await self._close(position, price, f"tp5_catastrophic_stop_{stop_pct:g}")
                    return
            if current_return >= target_pct:
                await self._close(position, price, f"tp5_profit_target_{target_pct:g}")
                return
            return

        # v1.3 tier-specific live strategy. Only positions opened with the new persisted
        # exit_strategy values use these rules. Existing pre-v1.3 positions retain their
        # legacy runner/protection behavior, avoiding a mid-trade strategy mutation.
        if position.exit_strategy in {"fixed_time_standard", "tp20_or_timeout"}:
            age_seconds = max(0.0, (datetime.now(UTC) - position.opened_at).total_seconds())

            if position.exit_strategy == "fixed_time_standard":
                if position.target_20_at is None and peak >= self.settings.profit_target_pct:
                    if await self.repo.mark_target_hit(position.id, price=price, return_pct=current_return):
                        position = (await self.repo.position(position.id)) or position
                        await self._notify(
                            "🎯 STANDARD +20% MILESTONE",
                            f"**{position.symbol}** reached +{self.settings.profit_target_pct:.0f}%. The Standard strategy still holds to {position.position_maturity}.",
                            [
                                {"name": "Current return", "value": f"{current_return:+.2f}%", "inline": True},
                                {"name": "Peak return", "value": f"{peak:+.2f}%", "inline": True},
                                {"name": "Exit rule", "value": f"Fixed hold to {position.position_maturity}", "inline": True},
                            ],
                            color=GREEN,
                        )
            elif current_return >= self.settings.profit_target_pct and position.target_20_at is None:
                await self.repo.mark_target_hit(position.id, price=price, return_pct=current_return)
                position = (await self.repo.position(position.id)) or position

            reason = tier_strategy_exit_reason(
                exit_strategy=position.exit_strategy,
                position_maturity=position.position_maturity,
                current_return_pct=current_return,
                age_seconds=age_seconds,
                profit_target_pct=self.settings.profit_target_pct,
            )
            if reason is not None:
                await self._close(position, price, reason)
                return
            # No runner floor/trailing stop for the new tier strategy. The initial
            # update_market call above already persisted this tick and adverse telemetry.
            return

        if position.target_20_at is None and peak >= self.settings.profit_target_pct:
            if await self.repo.mark_target_hit(position.id, price=price, return_pct=current_return):
                position = (await self.repo.position(position.id)) or position
                LOGGER.info(
                    "Profit target reached; runner remains open position_id=%s symbol=%s current_return=%.4f peak=%.4f",
                    position.id, position.symbol, current_return, peak,
                )
                await self._notify(
                    "🎯 +20% PROFIT MILESTONE REACHED",
                    f"**{position.symbol}** reached the +{self.settings.profit_target_pct:.0f}% short-return milestone. The runner stays open for additional upside.",
                    [
                        {"name": "Current return", "value": f"{current_return:+.2f}%", "inline": True},
                        {"name": "Peak return", "value": f"{peak:+.2f}%", "inline": True},
                        {"name": "Current price", "value": f"{price:.10g}", "inline": True},
                        {"name": "Entry", "value": f"{position.entry_price:.10g}", "inline": True},
                        {"name": "Next step", "value": f"Protection arms at +{self.settings.protection_arm_pct:.0f}% peak; then the +{self.settings.profit_target_pct:.0f}% floor / {self.settings.trail_callback_pct:.0f}% price trail manages the runner.", "inline": False},
                    ],
                    color=GREEN,
                )

        floor = protected_profit_floor_pct(
            peak_profit_pct=peak,
            hard_floor_pct=self.settings.profit_target_pct,
            arm_pct=self.settings.protection_arm_pct,
            trail_callback_pct=self.settings.trail_callback_pct,
        )
        existing_floor = position.profit_floor_pct
        if floor is not None:
            if position.protection_armed_at is None:
                order_id = None
                if position.mode == "live":
                    order_id = await self._place_live_protection(position, floor)
                if await self.repo.mark_protection_armed(
                    position.id, order_id=order_id, floor_pct=floor, price=price, return_pct=current_return
                ):
                    position = (await self.repo.position(position.id)) or position
                    existing_floor = floor
                    LOGGER.info(
                        "Profit protection armed position_id=%s symbol=%s peak=%.4f floor=%.4f",
                        position.id, position.symbol, peak, floor,
                    )
            elif existing_floor is None or floor >= existing_floor + self.settings.protection_update_step_pct:
                if position.mode == "live":
                    await self._update_live_protection(position, floor)
                await self.repo.set_protection(position.id, order_id=position.mexc_protection_order_id, floor_pct=floor)
                if self._should_notify_floor(position, floor):
                    await self.repo.patch_metadata(position.id, {"last_protection_notified_floor": floor})
                    LOGGER.info(
                        "Protected profit raised position_id=%s symbol=%s peak=%.4f floor=%.4f",
                        position.id, position.symbol, peak, floor,
                    )
                existing_floor = floor

        # Paper mode emulates the exchange-side stop. Live mode relies on MEXC's position stop and reconciliation.
        effective_floor = floor if floor is not None else existing_floor
        if position.mode == "paper" and position.protection_armed_at is not None and effective_floor is not None:
            if current_return <= effective_floor:
                await self._close(position, price, "protected_runner_exit")
                return

        await self.repo.update_market(
            position.id,
            price=price,
            return_pct=current_return,
            peak_profit_pct=peak,
            max_adverse_pct=adverse,
            profit_floor_pct=effective_floor,
        )

    async def _ensure_live_protection_exists(self, position: TraderPosition) -> None:
        now = time.monotonic()
        if now - self._last_protection_check.get(position.id, 0.0) < 30.0:
            return
        self._last_protection_check[position.id] = now
        if position.profit_floor_pct is None or position.mexc_position_id is None:
            return
        rows = await self.mexc.open_stop_orders(position.symbol)
        expected = int(position.mexc_protection_order_id or 0)
        found = next(
            (
                row for row in rows
                if int(row.get("positionId") or 0) == position.mexc_position_id
                and (expected == 0 or int(row.get("id") or 0) == expected)
                and int(row.get("state") or 1) == 1
            ),
            None,
        )
        if found:
            actual_id = int(found.get("id") or expected)
            if actual_id and actual_id != expected:
                await self.repo.set_protection(
                    position.id, order_id=actual_id, floor_pct=position.profit_floor_pct
                )
            return
        order_id = await self._place_live_protection(position, position.profit_floor_pct)
        await self.repo.set_protection(position.id, order_id=order_id, floor_pct=position.profit_floor_pct)
        LOGGER.error(
            "Live protection was missing and restored position_id=%s symbol=%s floor=%.4f new_stop_order_id=%s",
            position.id, position.symbol, position.profit_floor_pct, order_id,
        )
        await self._notify(
            "🚨 LIVE PROTECTION WAS MISSING — RESTORED",
            f"**{position.symbol}** had no active MEXC protection order. A replacement exchange-side stop was placed immediately.",
            [
                {
                    "name": "Protected level",
                    "value": (
                        f"{position.profit_floor_pct:+.2f}%"
                        if position.profit_floor_pct is not None else "n/a"
                    ),
                    "inline": True,
                },
                {"name": "New stop order", "value": str(order_id), "inline": True},
            ],
            color=ORANGE,
        )

    async def _place_live_protection(self, position: TraderPosition, floor_pct: float) -> int:
        if position.mexc_position_id is None:
            raise RuntimeError("cannot place live protection without MEXC position id")
        spec = await self.mexc.contract_spec(position.symbol)
        contracts = float(position.metadata.get("contracts") or position.quantity_base / spec.contract_size)
        stop_price = short_price_for_return(position.entry_price, floor_pct)
        return await self.mexc.place_position_stop(
            position_id=position.mexc_position_id, contracts=contracts, stop_price=stop_price
        )

    async def _update_live_protection(self, position: TraderPosition, floor_pct: float) -> None:
        if position.mexc_protection_order_id is None:
            order_id = await self._place_live_protection(position, floor_pct)
            await self.repo.set_protection(position.id, order_id=order_id, floor_pct=floor_pct)
            return
        await self.mexc.modify_position_stop(
            stop_order_id=position.mexc_protection_order_id,
            stop_price=short_price_for_return(position.entry_price, floor_pct),
        )

    def _should_notify_floor(self, position: TraderPosition, floor: float) -> bool:
        last = float(position.metadata.get("last_protection_notified_floor") or self.settings.profit_target_pct)
        return floor >= last + self.settings.protection_notify_step_pct

    async def _close(self, position: TraderPosition, price: float, reason: str) -> None:
        close_order_id: int | None = None
        exit_fee = 0.0
        if position.mode == "live":
            spec = await self.mexc.contract_spec(position.symbol)
            contracts = float(position.metadata.get("contracts") or position.quantity_base / spec.contract_size)
            close_order_id = await self.mexc.close_market_short(
                symbol=position.symbol,
                contracts=contracts,
                open_type=2 if "cross" in position.capital_strategy else 1,
                reference_price=price,
                position_id=position.mexc_position_id,
                external_oid=f"exh-close-{position.id}-{int(datetime.now(UTC).timestamp())}",
                leverage=self.settings.leverage,
            )
            await asyncio.sleep(0.6)
            order = await self.mexc.order(close_order_id)
            price = float(order.get("dealAvgPrice") or price)
            exit_fee = float(order.get("takerFee") or order.get("makerFee") or 0.0)
            if position.mexc_protection_order_id:
                try:
                    await self.mexc.cancel_position_stop(position.mexc_protection_order_id)
                except Exception:
                    LOGGER.warning("Could not cancel protection order after explicit close", exc_info=True)
        else:
            exit_notional = position.quantity_base * price
            exit_fee = exit_notional * self.settings.paper_taker_fee_rate

        await self.repo.close_position(
            position,
            exit_price=price,
            status="closed",
            reason=reason,
            exit_fee_usdt=exit_fee,
            mexc_close_order_id=close_order_id,
        )
        if position.mode == "paper":
            runtime = await self.repo.runtime()
            pnl = position.quantity_base * (position.entry_price - price)
            await self.repo.set_paper_equity(max(0.0, float(runtime["paper_equity_usdt"]) + pnl - exit_fee))
        await self.mexc.ticker_stream.remove(position.symbol)
        realized = short_return_pct(position.entry_price, price)
        gross_pnl = position.quantity_base * (position.entry_price - price)
        net_pnl = gross_pnl - position.entry_fee_usdt - exit_fee
        LOGGER.info(
            "Position closed id=%s symbol=%s reason=%s return_pct=%.4f net_pnl=%.4f exit_price=%.10g",
            position.id, position.symbol, reason, realized, net_pnl, price,
        )
        await self._notify(
            "💰 POSITION CLOSED",
            f"**{position.symbol}** • {reason}",
            [
                {"name": "Gross return", "value": f"{realized:+.2f}%", "inline": True},
                {"name": "Peak", "value": f"{max(position.peak_profit_pct, realized):+.2f}%", "inline": True},
                {"name": "Price P/L after fees", "value": f"${net_pnl:+,.4f}", "inline": True},
                {"name": "Fees", "value": f"${position.entry_fee_usdt + exit_fee:,.4f}", "inline": True},
                {"name": "Exit price", "value": f"{price:.10g}", "inline": True},
            ],
            color=GREEN if realized > 0 else RED,
        )

    async def _reconcile_live_positions(
        self, managed: list[TraderPosition], exchange_rows: list[dict[str, Any]]
    ) -> None:
        exchange_by_id = {int(r.get("positionId") or 0): r for r in exchange_rows}
        managed_ids = {p.mexc_position_id for p in managed if p.mode == "live" and p.mexc_position_id}
        extras = [r for r in exchange_rows if int(r.get("positionId") or 0) not in managed_ids]
        if extras and not self._live_execution_halted:
            self._live_execution_halted = True
            LOGGER.error("Live execution halted: unmanaged MEXC positions=%s", [r.get("symbol") for r in extras])
            await self._notify(
                "🚨 LIVE EXECUTION HALTED — UNMANAGED MEXC POSITION",
                "MEXC reports an open futures position that is not in the bot database. New entries are halted; the bot will not touch the unmanaged position.",
                [{"name": "Unmanaged", "value": ", ".join(str(r.get("symbol")) for r in extras)[:1024], "inline": False}],
                color=RED,
            )
        for position in managed:
            if position.mode != "live" or position.mexc_position_id is None:
                continue
            if position.mexc_position_id in exchange_by_id:
                continue
            await self._reconcile_exchange_closed(position)

    async def _reconcile_exchange_closed(self, position: TraderPosition) -> None:
        price = await self.mexc.last_price(position.symbol)
        reason = "exchange_position_closed"
        close_order_id = None
        exit_fee = 0.0
        try:
            rows = await self.mexc.stop_orders(symbol=position.symbol, finished=True)
            match = next((r for r in rows if int(r.get("id") or 0) == int(position.mexc_protection_order_id or 0) and int(r.get("state") or 0) == 3), None)
            if match:
                if position.exit_strategy == "tp5_sl75_full":
                    stop_pct = float(position.metadata.get("catastrophic_stop_pct") or self.settings.catastrophic_stop_pct)
                    reason = f"tp5_catastrophic_stop_{stop_pct:g}_exchange"
                else:
                    reason = "exchange_protection_stop"
                place_order_id = int(match.get("placeOrderId") or 0)
                if place_order_id:
                    order = await self.mexc.order(place_order_id)
                    price = float(order.get("dealAvgPrice") or price)
                    exit_fee = float(order.get("takerFee") or order.get("makerFee") or 0.0)
                    close_order_id = place_order_id
        except Exception:
            LOGGER.warning("Could not resolve exchange-side close details for %s", position.symbol, exc_info=True)
        await self.repo.close_position(
            position, exit_price=price, status="closed", reason=reason,
            exit_fee_usdt=exit_fee, mexc_close_order_id=close_order_id,
        )
        await self.mexc.ticker_stream.remove(position.symbol)
        LOGGER.info(
            "Exchange-side position close reconciled id=%s symbol=%s reason=%s exit_price=%.10g",
            position.id, position.symbol, reason, price,
        )
        protection_exit = reason == "exchange_protection_stop" or reason.startswith("tp5_catastrophic_stop_")
        await self._notify(
            "🛡️ EXCHANGE-SIDE EXIT CONFIRMED" if protection_exit else "🚨 MEXC POSITION CLOSED OUTSIDE BOT FLOW",
            f"**{position.symbol}** • {reason}",
            [
                {"name": "Exit", "value": f"{price:.10g}", "inline": True},
                {"name": "Return", "value": f"{short_return_pct(position.entry_price, price):+.2f}%", "inline": True},
            ],
            color=(RED if reason.startswith("tp5_catastrophic_stop_") else GREEN) if protection_exit else RED,
        )

    async def _account_equity(self, positions: list[TraderPosition] | None = None) -> float:
        if self.settings.trading_mode == "live":
            return await self.mexc.usdt_equity()
        runtime = await self.repo.runtime()
        realized_equity = float(runtime["paper_equity_usdt"])
        positions = positions if positions is not None else await self._active_positions()
        unrealized = sum(p.notional_usdt * p.current_return_pct / 100.0 for p in positions if p.mode == "paper")
        return realized_equity + unrealized

    async def _snapshot_fields(self) -> list[dict[str, Any]]:
        positions = await self._active_positions()
        equity = await self._account_equity(positions)
        fields = await self._build_snapshot_fields(positions, equity=equity, equity_is_live=True)
        self._last_snapshot_fields = fields
        return fields

    async def _db_snapshot_fields(self, snapshot_error: Exception | None = None) -> list[dict[str, Any]]:
        """Build a status snapshot without requiring MEXC network access.

        This is intentionally usable during an exchange/API outage so error alerts still contain
        the last database-observed positions and P/L instead of becoming context-free messages.
        """
        positions = await self._active_positions()
        runtime = await self.repo.runtime()
        if self.settings.trading_mode == "paper":
            realized = float(runtime["paper_equity_usdt"])
            equity = realized + sum(
                p.notional_usdt * p.current_return_pct / 100.0 for p in positions if p.mode == "paper"
            )
            live_equity = True
        else:
            equity = None
            live_equity = False
        fields = await self._build_snapshot_fields(positions, equity=equity, equity_is_live=live_equity)
        if snapshot_error is not None:
            fields.insert(
                0,
                {
                    "name": "⚠️ Snapshot source",
                    "value": (
                        "MEXC live equity was unavailable; showing the last database-observed portfolio state. "
                        f"`{type(snapshot_error).__name__}: {str(snapshot_error)[:300]}`"
                    )[:1024],
                    "inline": False,
                },
            )
        self._last_snapshot_fields = fields
        return fields

    async def _build_snapshot_fields(
        self,
        positions: list[TraderPosition],
        *,
        equity: float | None,
        equity_is_live: bool,
    ) -> list[dict[str, Any]]:
        runtime = await self.repo.runtime()
        stats = await self.repo.portfolio_stats(self._active_run_id)
        total_notional = sum(p.notional_usdt for p in positions)
        exposure = total_notional / equity * 100.0 if equity is not None and equity > 0 else None
        closed = int(stats.get("closed_count") or 0)
        wins = int(stats.get("win_count") or 0)
        win_rate = wins / closed * 100.0 if closed else 0.0
        if equity is None:
            account_value = "Live equity **unavailable** • DB position P/L shown below"
        else:
            account_value = f"Equity/MTM **${equity:,.2f}**"
        if self.settings.trading_mode == "paper":
            account_value += f"\nRealized cash **${float(runtime['paper_equity_usdt']):,.2f}**"
        elif not equity_is_live:
            account_value += "\nMEXC account endpoint unavailable"
        account_value += (
            f"\nClosed {closed} • wins {wins} ({win_rate:.1f}%) • "
            f"liquidations {int(stats.get('liquidation_count') or 0)} • fees ${float(stats.get('fees') or 0):,.4f}"
        )
        exposure_text = f"{exposure:.2f}%" if exposure is not None else "n/a"
        if self.settings.uses_generic_slots:
            capacity = (
                f"Slots **{len(positions)}/{self.settings.max_open_positions}** • generic STANDARD + HIGH\n"
                f"Open notional **${total_notional:,.2f}** • exposure **{exposure_text} / "
                f"{self.settings.max_total_exposure_pct:.2f}%**"
            )
        else:
            capacity = (
                f"Slots **{len(positions)}/{self.settings.max_open_positions}** • STD "
                f"**{sum(p.risk_tier == 'STANDARD' for p in positions)}/{self.settings.max_standard_positions}** • HIGH "
                f"**{sum(p.risk_tier == 'HIGH_RISK' for p in positions)}/{self.settings.max_high_risk_positions}**\n"
                f"Open notional **${total_notional:,.2f}** • exposure **{exposure_text} / "
                f"{self.settings.max_total_exposure_pct:.2f}%**"
            )
        if positions:
            lines = []
            for p in positions[:10]:
                if p.exit_strategy == "tp5_sl75_full":
                    stop_pct = float(p.metadata.get("catastrophic_stop_pct") or self.settings.catastrophic_stop_pct)
                    floor = f" • SL -{stop_pct:.0f}%"
                else:
                    floor = f" • floor +{p.profit_floor_pct:.1f}%" if p.profit_floor_pct is not None else ""
                tier = "STD" if p.risk_tier == "STANDARD" else "HIGH" if p.risk_tier == "HIGH_RISK" else "EXT"
                breach = (
                    " • ⚫400" if p.breach_400_at else " • 🟣300" if p.breach_300_at
                    else " • 🟥200" if p.breach_200_at else " • 🔴100" if p.breach_100_at else ""
                )
                lines.append(
                    f"`#{p.slot_no or '-'} {p.symbol}` {tier} **{p.current_return_pct:+.1f}%** • "
                    f"peak {p.peak_profit_pct:+.1f}% • adv {p.max_adverse_pct:.1f}%{floor}{breach}"
                )
            open_text = "\n".join(lines)[:1024]
        else:
            open_text = "No open positions."
        return [
            {"name": "📊 Account", "value": account_value[:1024], "inline": True},
            {"name": "🎛️ Capacity", "value": capacity[:1024], "inline": True},
            {"name": "📂 Open positions", "value": open_text, "inline": False},
        ]

    async def _notify(
        self, title: str, description: str, event_fields: list[dict[str, Any]] | None = None,
        *, color: int | None = None,
    ) -> None:
        try:
            snapshot = await self._snapshot_fields()
        except Exception as exc:
            LOGGER.warning("Could not build live trader notification snapshot: %s", exc)
            try:
                snapshot = await self._db_snapshot_fields(exc)
            except Exception:
                LOGGER.warning("Could not build DB fallback trader snapshot", exc_info=True)
                snapshot = self._last_snapshot_fields or [
                    {
                        "name": "⚠️ Portfolio status",
                        "value": "Current portfolio snapshot unavailable. The scanner watchdog remains independent.",
                        "inline": False,
                    }
                ]
        fields = list(event_fields or []) + snapshot
        await self.notifier.send(title, description, fields, color=color)

    async def _alert_error(self, key: str, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_error_alert.get(key, 0.0) < self.settings.error_alert_cooldown_seconds:
            return
        self._last_error_alert[key] = now
        await self._notify(
            "🚨 TRADER / SERVER ERROR",
            f"**{key}** failed. The trader continues running and will retry automatically.\n`{type(exc).__name__}: {str(exc)[:700]}`",
            color=RED,
        )

    async def _maybe_heartbeat(self) -> None:
        now = time.monotonic()
        if now - self._last_discord_heartbeat < self.settings.heartbeat_seconds:
            return
        self._last_discord_heartbeat = now
        LOGGER.info(
            "Trader heartbeat healthy strategy=%s",
            self._strategy_label(),
        )

    async def _write_db_heartbeat(self) -> None:
        positions = await self._active_positions()
        await self.db.heartbeat(
            "portfolio_short_trader",
            {
                "mode": self.settings.trading_mode,
                "allowed_risks": list(self.settings.allowed_risk_tiers),
                "slots": self.settings.max_open_positions,
                "open_count": len(positions),
                "slot_allocation_pct": self.settings.slot_allocation_pct,
                "max_total_exposure_pct": self.settings.max_total_exposure_pct,
                "strategy": self.settings.execution_strategy,
                "run_id": self._active_run_id,
                "version": "1.3.45",
            },
        )

    def _strategy_label(self) -> str:
        if self.settings.uses_generic_slots:
            stop = (
                f" • catastrophic SL -{self.settings.catastrophic_stop_pct:g}%"
                if self.settings.uses_catastrophic_stop else ""
            )
            if self.settings.uses_pcr_derisk:
                sizing = "PCR 2.50% flagged / 5.00% otherwise"
            elif self.settings.uses_htf_derisk:
                sizing = "HTF V1 2.50% flagged / 5.00% otherwise"
            else:
                sizing = f"{self.settings.slot_allocation_pct:.2f}%"
            return (
                f"{self.settings.execution_strategy.upper()} • {self.settings.max_open_positions} generic slots × "
                f"{sizing} • TP +{self.settings.tp5_target_pct:g}%{stop} • "
                f"max {self.settings.max_total_exposure_pct:.1f}% exposure • one position/symbol"
            )
        return (
            f"{'+'.join(self.settings.allowed_risk_tiers)} • "
            f"STD {self.settings.max_standard_positions}×{self.settings.standard_hold_days}d + "
            f"HIGH {self.settings.max_high_risk_positions}×TP{self.settings.profit_target_pct:.0f}/"
            f"{self.settings.high_risk_timeout_days}d • {self.settings.max_open_positions} slots × "
            f"{self.settings.slot_allocation_pct:.2f}% • max {self.settings.max_total_exposure_pct:.1f}% exposure"
        )


# Backward-compatible import name used by older tests/deploy scripts.
StandardShortTrader = PortfolioShortTrader


async def main() -> None:
    settings = TraderSettings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    trader = PortfolioShortTrader(settings)
    await trader.run()


if __name__ == "__main__":
    asyncio.run(main())

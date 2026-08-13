from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from app.db import Database
from app.mexc_trade import MexcTradeClient, MexcTradeError
from app.trader_config import TraderSettings
from app.trader_db import TraderRepository
from app.trader_logic import maturity_exit_reason, short_return_pct
from app.trader_models import TradeSignal, TraderPosition
from app.trader_notifier import TraderNotifier

LOGGER = logging.getLogger(__name__)


class StandardShortTrader:
    def __init__(self, settings: TraderSettings) -> None:
        self.settings = settings
        self.db = Database(settings.database_url)
        self.repo = TraderRepository(self.db)
        self.mexc = MexcTradeClient(
            settings.mexc_base_url,
            api_key=settings.mexc_api_key,
            api_secret=settings.mexc_api_secret,
        )
        self.notifier = TraderNotifier(settings.trader_discord_webhook_url)

    async def start(self) -> None:
        await self.db.connect()
        await self.db.migrate()
        await self.repo.initialize_runtime(
            starting_equity=self.settings.paper_starting_equity_usdt,
            process_existing=self.settings.process_existing_signals,
        )
        LOGGER.info(
            "Trader started mode=%s capital=%s maturity=%s only_risk=STANDARD",
            self.settings.trading_mode,
            self.settings.capital_strategy,
            self.settings.position_maturity,
        )

    async def close(self) -> None:
        await asyncio.gather(
            self.mexc.close(),
            self.notifier.close(),
            self.db.close(),
            return_exceptions=True,
        )

    async def run(self) -> None:
        await self.start()
        try:
            while True:
                try:
                    await self.tick()
                except Exception:
                    LOGGER.exception("Trader tick failed")
                await asyncio.sleep(self.settings.poll_seconds)
        finally:
            await self.close()

    async def tick(self) -> None:
        position = await self.repo.active_position()
        if position is not None:
            await self._monitor_position(position)
        await self._consume_new_signals()
        runtime = await self.repo.runtime()
        position = await self.repo.active_position()
        await self.db.heartbeat(
            "standard_short_trader",
            {
                "mode": self.settings.trading_mode,
                "capital_strategy": self.settings.capital_strategy,
                "position_maturity": self.settings.position_maturity,
                "paper_equity_usdt": runtime["paper_equity_usdt"],
                "open_position": position.symbol if position else None,
                "version": "1.1.1",
            },
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
            finally:
                await self.repo.set_cursor(signal.id)

    async def _handle_signal(self, signal: TradeSignal) -> None:
        if signal.risk_tier != "STANDARD":
            await self.repo.decision(
                signal.id,
                "ignored_risk",
                f"risk tier {signal.risk_tier or 'UNKNOWN'} is not STANDARD",
            )
            return

        age = (datetime.now(UTC) - signal.signaled_at).total_seconds()
        if age > self.settings.max_signal_age_seconds:
            await self.repo.decision(
                signal.id,
                "ignored_stale",
                f"signal age {age:.0f}s exceeds {self.settings.max_signal_age_seconds}s",
            )
            return

        active = await self.repo.active_position()
        if active is not None:
            await self.repo.decision(
                signal.id,
                "ignored_busy",
                f"single-position rule: {active.symbol} position #{active.id} is open",
            )
            LOGGER.info("Ignored %s because %s is already open", signal.symbol, active.symbol)
            return

        if self.settings.trading_mode == "paper":
            position = await self._open_paper(signal)
        else:
            position = await self._open_live(signal)
        LOGGER.info(
            "Opened %s short %s at %.10g notional=%.2f equity=%.2f",
            position.mode,
            position.symbol,
            position.entry_price,
            position.notional_usdt,
            position.entry_equity_usdt,
        )
        await self.notifier.send(
            "🧾 PAPER SHORT OPENED" if position.mode == "paper" else "🔴 LIVE SHORT OPENED",
            f"**{position.symbol}** • STANDARD signal",
            [
                {"name": "Entry", "value": f"{position.entry_price:.10g}", "inline": True},
                {"name": "Notional", "value": f"${position.notional_usdt:,.2f}", "inline": True},
                {"name": "Margin", "value": position.capital_strategy, "inline": True},
                {"name": "Maturity", "value": position.position_maturity, "inline": True},
                {"name": "+20% target", "value": f"+{self.settings.profit_target_pct:.0f}%", "inline": True},
            ],
        )

    async def _open_paper(self, signal: TradeSignal) -> TraderPosition:
        price = await self.mexc.last_price(signal.symbol)
        runtime = await self.repo.runtime()
        equity = float(runtime["paper_equity_usdt"])
        if equity <= 0:
            raise RuntimeError("paper account equity is zero")
        notional = equity * self.settings.position_fraction
        quantity = notional / price
        return await self.repo.create_position(
            signal=signal,
            mode="paper",
            capital_strategy=self.settings.capital_strategy,
            exit_strategy="ratchet_5",  # legacy DB field; v1.1 execution is maturity-driven
            position_maturity=self.settings.position_maturity,
            entry_price=price,
            entry_equity_usdt=equity,
            notional_usdt=notional,
            quantity_base=quantity,
            liquidation_proxy_pct=self.settings.liquidation_proxy_pct,
            metadata={
                "signal_entry_hint": signal.entry_hint,
                "position_fraction": self.settings.position_fraction,
                "leverage": 1,
                "risk_tier": signal.risk_tier,
            },
        )

    async def _open_live(self, signal: TradeSignal) -> TraderPosition:
        existing = await self.mexc.open_positions()
        if existing:
            raise RuntimeError("MEXC already has an open futures position; refusing a new live trade")
        price = await self.mexc.last_price(signal.symbol)
        spec = await self.mexc.contract_spec(signal.symbol)
        if not spec.api_allowed:
            raise MexcTradeError(f"{signal.symbol} reports apiAllowed=false")
        required_open_type = self.settings.open_type
        if spec.position_open_type not in {required_open_type, 3}:
            raise MexcTradeError(
                f"{signal.symbol} does not support requested margin mode {required_open_type}"
            )
        equity = await self.mexc.usdt_equity()
        notional = equity * self.settings.position_fraction
        contracts = spec.contracts_for_notional(notional, price)
        order_id = await self.mexc.submit_market_short(
            symbol=signal.symbol,
            contracts=contracts,
            open_type=required_open_type,
            reference_price=price,
            external_oid=f"std-short-{signal.id}",
        )
        live_position: dict[str, Any] | None = None
        for _ in range(10):
            await asyncio.sleep(0.5)
            rows = await self.mexc.open_positions(signal.symbol)
            live_position = next(
                (row for row in rows if int(row.get("positionType") or 0) == 2),
                None,
            )
            if live_position:
                break
        if not live_position:
            raise MexcTradeError(
                f"MEXC order {order_id} submitted but short position could not be confirmed"
            )
        entry_price = float(live_position.get("openAvgPrice") or live_position.get("holdAvgPrice") or price)
        hold_contracts = float(live_position.get("holdVol") or contracts)
        return await self.repo.create_position(
            signal=signal,
            mode="live",
            capital_strategy=self.settings.capital_strategy,
            exit_strategy="ratchet_5",  # legacy DB field; v1.1 execution is maturity-driven
            position_maturity=self.settings.position_maturity,
            entry_price=entry_price,
            entry_equity_usdt=equity,
            notional_usdt=hold_contracts * spec.contract_size * entry_price,
            quantity_base=hold_contracts * spec.contract_size,
            liquidation_proxy_pct=self.settings.liquidation_proxy_pct,
            mexc_position_id=int(live_position["positionId"]),
            mexc_open_order_id=order_id,
            metadata={
                "contracts": hold_contracts,
                "contract_size": spec.contract_size,
                "position_fraction": self.settings.position_fraction,
                "leverage": 1,
                "risk_tier": signal.risk_tier,
                "mexc_liquidate_price": live_position.get("liquidatePrice"),
            },
        )

    async def _monitor_position(self, position: TraderPosition) -> None:
        price = await self.mexc.last_price(position.symbol)
        current_return = short_return_pct(position.entry_price, price)
        peak = max(position.peak_profit_pct, current_return)
        adverse = max(position.max_adverse_pct, max(0.0, -current_return))

        # Paper liquidation proxy always has priority. In live mode the exchange
        # itself determines liquidation; the local research threshold is not used
        # as an order trigger.
        if position.mode == "paper" and adverse >= position.liquidation_proxy_pct:
            await self._close(position, price, "liquidation_proxy", liquidated=True)
            return

        age_seconds = (datetime.now(UTC) - position.opened_at).total_seconds()
        exit_reason = maturity_exit_reason(
            position_maturity=position.position_maturity,
            current_return_pct=current_return,
            age_seconds=age_seconds,
            profit_target_pct=self.settings.profit_target_pct,
        )
        if exit_reason is not None:
            await self._close(position, price, exit_reason)
            return

        await self.repo.update_market(
            position.id,
            price=price,
            return_pct=current_return,
            peak_profit_pct=peak,
            max_adverse_pct=adverse,
            profit_floor_pct=None,
        )

    async def _close(
        self,
        position: TraderPosition,
        price: float,
        reason: str,
        *,
        liquidated: bool = False,
    ) -> None:
        close_order_id: int | None = None
        if position.mode == "live":
            spec = await self.mexc.contract_spec(position.symbol)
            contracts = float(position.metadata.get("contracts") or (position.quantity_base / spec.contract_size))
            close_order_id = await self.mexc.close_market_short(
                symbol=position.symbol,
                contracts=contracts,
                open_type=1 if position.capital_strategy == "isolated_full" else 2,
                reference_price=price,
                position_id=position.mexc_position_id,
                external_oid=f"std-close-{position.id}-{int(datetime.now(UTC).timestamp())}",
            )
            await asyncio.sleep(0.5)
            try:
                order = await self.mexc.order(close_order_id)
                price = float(order.get("dealAvgPrice") or price)
            except Exception:
                LOGGER.exception("Could not fetch close fill; using reference price")

        status = "liquidated" if liquidated else "closed"
        closed = await self.repo.close_position(
            position,
            exit_price=price,
            status=status,
            reason=reason,
            mexc_close_order_id=close_order_id,
        )
        if position.mode == "paper":
            runtime = await self.repo.runtime()
            pnl = position.quantity_base * (position.entry_price - price)
            new_equity = max(0.0, float(runtime["paper_equity_usdt"]) + pnl)
            await self.repo.set_paper_equity(new_equity)
        else:
            new_equity = await self.mexc.usdt_equity()

        realized = short_return_pct(position.entry_price, price)
        LOGGER.info(
            "Closed %s %s reason=%s return=%+.2f%% equity=%.2f",
            position.mode,
            position.symbol,
            reason,
            realized,
            new_equity,
        )
        await self.notifier.send(
            "💰 SHORT CLOSED" if not liquidated else "💥 POSITION LIQUIDATED (PAPER PROXY)",
            f"**{position.symbol}** • {reason}",
            [
                {"name": "Return", "value": f"{realized:+.2f}%", "inline": True},
                {"name": "Exit", "value": f"{price:.10g}", "inline": True},
                {"name": "Equity", "value": f"${new_equity:,.2f}", "inline": True},
            ],
        )


async def main() -> None:
    settings = TraderSettings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    trader = StandardShortTrader(settings)
    await trader.run()


if __name__ == "__main__":
    asyncio.run(main())

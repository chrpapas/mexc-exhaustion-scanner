"""MEXC live order transport, completely isolated from the T100 paper portfolio.

Save intent before HTTP POST. Ambiguous submissions must be reconciled by
externalOid and MUST NOT be retried automatically. Not a strategy scheduler:
V40 VOL30_SKIP+H4 parity, dynamic exposure and native trailing exits require
separate certification before autonomous live operation.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Protocol

from app.t100_live_active_tpsl import effective_protective_quantity
from app.t100_live_authorization import (
    LiveMutationPermit,
    require_certified_transport_permit,
)


class ExecutionHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class LiveCaps:
    max_order_usdt: Decimal
    max_portfolio_usdt: Decimal
    max_daily_loss_usdt: Decimal
    max_slippage_bps: Decimal = Decimal("100")
    max_book_age_ms: int = 1500
    leverage: int = 1
    margin_type: int = 1

    def __post_init__(self) -> None:
        for field in ("max_order_usdt", "max_portfolio_usdt", "max_daily_loss_usdt"):
            v = getattr(self, field)
            if not v.is_finite() or v <= 0:
                raise ValueError(f"invalid cap {field}")
        if not Decimal(0) <= self.max_slippage_bps <= Decimal(300):
            raise ValueError("unsafe slippage setting")
        if self.max_book_age_ms < 0 or self.max_book_age_ms > 2000:
            raise ValueError("book age limit out of bounds")
        if self.leverage != 1 or self.margin_type != 1:
            raise ValueError("initial live adapter supports isolated 1x only")


@dataclass(frozen=True)
class EntryIntent:
    signal_id: int
    symbol: str
    reference_price: Decimal
    notional_usdt: Decimal
    volume_contracts: Decimal
    stop_price: Decimal
    external_oid: str

    @classmethod
    def build(cls, *, signal_id: int, symbol: str,
              reference_price: str | Decimal, notional_usdt: str | Decimal,
              contract_size: str | Decimal, volume_step: str | Decimal,
              min_volume: str | Decimal, max_volume: str | Decimal,
              price_tick: str | Decimal, stop_pct: Decimal = Decimal("75")) -> "EntryIntent":
        price, notional = Decimal(reference_price), Decimal(notional_usdt)
        size, step = Decimal(contract_size), Decimal(volume_step)
        minimum, maximum = Decimal(min_volume), Decimal(max_volume)
        tick = Decimal(price_tick)
        if re.fullmatch(r"[A-Z0-9]+_USDT", symbol) is None or symbol in {"BTC_USDT", "ETH_USDT"}:
            raise ValueError("symbol not permitted")
        if signal_id < 1 or any(
            not x.is_finite() or x <= 0 for x in (price, notional, size, step, minimum, maximum, tick)
        ):
            raise ValueError("invalid entry fields")
        if stop_pct != 75:
            raise ValueError("stop differs from frozen strategy")
        vol = (notional / (price * size) / step).to_integral_value(rounding=ROUND_DOWN) * step
        if vol < minimum or vol > maximum:
            raise ValueError("order below minimum or above maximum; no silent clip")
        oid = "t100v1e" + hashlib.sha256(f"{signal_id}:{symbol}".encode()).hexdigest()[:20]
        stop = (price * Decimal("1.75") / tick).to_integral_value(rounding=ROUND_UP) * tick
        return cls(signal_id, symbol, price, notional, vol, stop, oid)


def displayed_vwap(*, levels: list[list[Any]], contract_size: Decimal,
                   requested_contracts: Decimal, side: str) -> tuple[Decimal, Decimal]:
    """Calculate displayed-depth VWAP (levels are priced contract counts)."""
    if side not in {"sell", "buy"}:
        raise ValueError("invalid side")
    remaining, cost, filled = requested_contracts, Decimal(0), Decimal(0)
    if remaining <= 0 or contract_size <= 0:
        raise ValueError("invalid depth calculation")
    prices: list[Decimal] = []
    for price_raw, qty_raw, *_ in levels:
        price, qty = Decimal(str(price_raw)), Decimal(str(qty_raw))
        if not price.is_finite() or not qty.is_finite() or price <= 0 or qty < 0:
            raise ValueError("invalid order book")
        if qty == 0:
            continue
        prices.append(price)
        take = min(remaining, qty)
        cost += take * contract_size * price
        filled += take
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0:
        raise ExecutionHalted("INSUFFICIENT_DISPLAYED_DEPTH")
    if any((prices[i] < prices[i+1] if side == "sell" else prices[i] > prices[i+1])
           for i in range(len(prices)-1)):
        raise ExecutionHalted("UNSORTED_ORDER_BOOK")
    return cost / (filled * contract_size), filled


def adverse_bps(vwap: Decimal, reference: Decimal, side: str) -> Decimal:
    if reference <= 0:
        raise ValueError("reference must be positive")
    return (reference - vwap if side == "sell" else vwap - reference) / reference * Decimal(10000)


class IntentLedger(Protocol):
    async def insert_prepared(self, intent: EntryIntent) -> bool: ...
    async def transition(self, oid: str, expected: str, next_state: str,
                         *, order_id: str | None = None, reason: str | None = None) -> bool: ...
    async def state(self, oid: str) -> str: ...


class ExchangeClient(Protocol):
    async def order_by_external_oid(self, symbol: str, external_oid: str) -> dict[str, Any] | None: ...
    async def submit_short_with_stop(
        self,
        intent: EntryIntent,
        *,
        permit: LiveMutationPermit,
    ) -> str: ...
    async def open_positions(self, symbol: str | None = None) -> list[dict[str, Any]]: ...
    async def open_stop_orders(self, symbol: str | None = None) -> list[dict[str, Any]]: ...


def modeled_short_loss_at_native_stop(
    intent: EntryIntent, *,
    max_adverse_slippage_bps: Decimal,
    planning_fee_rate_per_side: Decimal = Decimal("0.001"),
) -> Decimal:
    """Conservative PLANNING estimate, never a guaranteed maximum loss.

    Frozen V40 short SL is at least 75% above its reference entry.
    Model entry and stop-exit friction on their corresponding notional
    amounts. Actual gap-through, liquidation, fees, market illiquidity,
    exchange incidents and unfilled protection may produce larger losses.
    This check prevents an obvious underbudget order, NOT a safe live pilot.
    """
    if type(intent) is not EntryIntent:
        raise ExecutionHalted("MODELED_STOP_LOSS_INTENT_INVALID")
    inputs = (
        intent.reference_price, intent.stop_price, intent.notional_usdt,
        max_adverse_slippage_bps, planning_fee_rate_per_side,
    )
    if any(
        type(v) is not Decimal or not v.is_finite()
        for v in inputs
    ):
        raise ExecutionHalted("MODELED_STOP_LOSS_INPUT_INVALID")
    entry, stop, notional, slip, fee = inputs
    if (
        entry <= 0 or stop <= 0 or notional <= 0
        or stop < entry * Decimal("1.75")
        or not Decimal("0") <= slip <= Decimal("300")
        or not Decimal("0.001") <= fee <= Decimal("0.01")
    ):
        raise ExecutionHalted("MODELED_STOP_LOSS_INPUT_OR_FROZEN_STOP_INVALID")
    exit_notional = notional * stop / entry
    gross_adverse = exit_notional - notional
    round_trip_friction = (
        (notional + exit_notional)
        * (slip / Decimal("10000") + fee)
    )
    return gross_adverse + round_trip_friction


class LiveOrderGateway:
    def __init__(self, ledger: IntentLedger, exchange: ExchangeClient, caps: LiveCaps) -> None:
        self.ledger, self.exchange, self.caps = ledger, exchange, caps

    async def send_one(self, intent: EntryIntent, *, permit: LiveMutationPermit,
                       equity_usdt: Decimal, portfolio_notional: Decimal,
                       today_loss_usdt: Decimal, observed_vwap: Decimal,
                       book_age_ms: int, signal_age_seconds: float) -> str:
        """This method performs a REAL POST after validated independent inputs.

        A future coordinator must obtain the inputs from fresh exchange/book
        snapshots, reconcile the account and enforce strategy parity and kill
        switches. Do not invoke this method without that coordinator.
        """
        permit = require_certified_transport_permit(permit)
        if intent.notional_usdt > permit.max_position_notional_usdt:
            raise ExecutionHalted("PERMIT_ORDER_CAP")
        if (
            portfolio_notional + intent.notional_usdt
            > permit.max_total_notional_usdt
        ):
            raise ExecutionHalted("PERMIT_PORTFOLIO_CAP")
        if today_loss_usdt >= permit.max_daily_loss_usdt:
            raise ExecutionHalted("PERMIT_DAILY_LOSS_CAP")
        if permit.leverage != self.caps.leverage:
            raise ExecutionHalted("PERMIT_LEVERAGE_MISMATCH")

        if intent.notional_usdt > self.caps.max_order_usdt:
            raise ExecutionHalted("ORDER_CAP")
        if portfolio_notional + intent.notional_usdt > self.caps.max_portfolio_usdt:
            raise ExecutionHalted("PORTFOLIO_CAP")
        if today_loss_usdt >= self.caps.max_daily_loss_usdt:
            raise ExecutionHalted("DAILY_LOSS_CAP")
        # A 75% frozen SL can consume most of a tiny allocation. The
        # prospective modeled stop loss MUST fit BOTH daily budgets before
        # persisting intent or making any exchange request.
        if (
            type(today_loss_usdt) is not Decimal
            or not today_loss_usdt.is_finite()
            or today_loss_usdt < 0
        ):
            raise ExecutionHalted("DAILY_LOSS_WITNESS_INVALID")
        planned_stop_loss = modeled_short_loss_at_native_stop(
            intent, max_adverse_slippage_bps=self.caps.max_slippage_bps,
        )
        if today_loss_usdt + planned_stop_loss >= min(
            self.caps.max_daily_loss_usdt,
            permit.max_daily_loss_usdt,
        ):
            raise ExecutionHalted("PROSPECTIVE_STOP_LOSS_EXCEEDS_DAILY_BUDGET")
        if equity_usdt <= 0 or intent.notional_usdt > equity_usdt * Decimal("0.5625"):
            raise ExecutionHalted("MARGIN_OR_EQUITY_CAP")
        if not 0 <= signal_age_seconds <= 900:
            raise ExecutionHalted("STALE_SIGNAL")
        if not 0 <= book_age_ms <= self.caps.max_book_age_ms:
            raise ExecutionHalted("STALE_BOOK")
        if adverse_bps(observed_vwap, intent.reference_price, "sell") > self.caps.max_slippage_bps:
            raise ExecutionHalted("ADVERSE_SLIPPAGE")
        existing = await self.exchange.open_positions()
        if existing:
            raise ExecutionHalted("ACCOUNT_HAS_OPEN_POSITIONS_REQUIRE_RECONCILIATION")
        if not await self.ledger.insert_prepared(intent):
            raise ExecutionHalted("DUPLICATE_INTENT_OR_SIGNAL")
        if not await self.ledger.transition(intent.external_oid, "PREPARED", "SUBMITTING"):
            raise ExecutionHalted("ORDER_INTENT_CAS_FAILED")
        try:
            # stopLossPrice is attached in the same MEXC create request.
            order_id = await self.exchange.submit_short_with_stop(
                intent,
                permit=permit,
            )
        except Exception as exc:
            await self.ledger.transition(intent.external_oid, "SUBMITTING", "UNKNOWN",
                                         reason=type(exc).__name__)
            raise ExecutionHalted(
                "SUBMISSION_UNKNOWN: reconcile by externalOid; NEVER blindly retry"
            ) from exc
        if not await self.ledger.transition(intent.external_oid, "SUBMITTING", "ACK",
                                            order_id=order_id):
            raise ExecutionHalted("ORDER_ACK_LEDGER_FAILURE_REQUIRES_MANUAL_RECONCILIATION")
        return order_id

    async def reconcile(self, intent: EntryIntent) -> str:
        """Read external order and native stop: missing order is NOT retry permission."""
        state = await self.ledger.state(intent.external_oid)
        if state not in {"SUBMITTING", "UNKNOWN", "ACK", "REVIEW"}:
            raise ExecutionHalted("UNEXPECTED_LEDGER_STATE")
        try:
            order = await self.exchange.order_by_external_oid(intent.symbol, intent.external_oid)
        except Exception as exc:
            raise ExecutionHalted("ORDER_LOOKUP_FAILED") from exc
        if not order:
            if state == "SUBMITTING":
                await self.ledger.transition(intent.external_oid, "SUBMITTING", "UNKNOWN",
                                             reason="external_oid_unresolved")
            raise ExecutionHalted("NO_ORDER_FOUND_BUT_POST_MAY_HAVE_SUCCEEDED")
        if str(order.get("symbol")) != intent.symbol or str(order.get("externalOid")) != intent.external_oid:
            raise ExecutionHalted("ORDER_IDENTITY_MISMATCH")
        if int(order.get("side", -1)) != 3:
            raise ExecutionHalted("ORDER_SIDE_MISMATCH")
        dealt = Decimal(str(order.get("dealVol") or 0))
        if dealt < 0 or dealt > intent.volume_contracts:
            raise ExecutionHalted("ORDER_FILL_VOLUME_MISMATCH")
        if state in {"SUBMITTING", "UNKNOWN"}:
            await self.ledger.transition(intent.external_oid, state, "ACK",
                                         order_id=str(order["orderId"]))
        if dealt > 0:
            if int(order.get("state", 0)) != 3 or dealt != intent.volume_contracts:
                raise ExecutionHalted("PARTIAL_OR_NONFINAL_FILL_REQUIRES_MANUAL_INTERVENTION")
            position_id = str(order.get("positionId") or "")
            if not position_id:
                raise ExecutionHalted("FILL_WITHOUT_POSITION_ID")
            try:
                stops = await self.exchange.open_stop_orders(intent.symbol)
            except Exception as exc:
                raise ExecutionHalted("STOP_QUERY_FAILED") from exc
            protected = any(
                str(s.get("positionId")) == position_id
                and int(s.get("state", 0)) == 1
                and Decimal(str(s.get("stopLossPrice") or "0")) > 0
                and Decimal(str(s.get("stopLossPrice") or "0")) <= intent.stop_price
                and (
                    effective_protective_quantity(
                        s,
                        position_quantity=dealt,
                    )
                    is not None
                )
                and effective_protective_quantity(
                    s,
                    position_quantity=dealt,
                ) >= dealt
                for s in stops
            )
            if not protected:
                raise ExecutionHalted("UNPROTECTED_REAL_POSITION_MANUAL_INTERVENTION_REQUIRED")
        return "VERIFIED_PROTECTED" if dealt > 0 else "ACK_UNFILLED"

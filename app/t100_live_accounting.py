"""Deterministic short-perpetual execution accounting (offline, no API calls).

This is a *reconciliation calculator*, not a venue adapter or live-ready PnL
ledger. Inputs must be complete, deduplicated, exchange-confirmed fill and
funding records in consistent settlement currency. Any uncertainty fails closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable


class AccountingHalted(RuntimeError):
    pass


def number(value: object, *, zero_ok: bool = False) -> Decimal:
    try:
        n = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise AccountingHalted("INVALID_ACCOUNTING_NUMBER") from exc
    if not n.is_finite() or n < 0 or (n == 0 and not zero_ok):
        raise AccountingHalted("INVALID_ACCOUNTING_NUMBER")
    return n


def signed(value: object) -> Decimal:
    try:
        n = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise AccountingHalted("INVALID_SIGNED_AMOUNT") from exc
    if not n.is_finite():
        raise AccountingHalted("INVALID_SIGNED_AMOUNT")
    return n


@dataclass(frozen=True)
class ExecutionFill:
    trade_id: str
    order_id: str
    position_id: str
    side: str   # OPEN_SHORT or CLOSE_SHORT, never inferred from sign
    contracts: Decimal
    price: Decimal
    fee_usdt: Decimal


@dataclass(frozen=True)
class FundingEvent:
    event_id: str
    position_id: str
    cashflow_usdt: Decimal  # positive is credited to account


@dataclass(frozen=True)
class ShortReconciliation:
    position_id: str
    opened_contracts: Decimal
    closed_contracts: Decimal
    remaining_contracts: Decimal
    open_notional_usdt: Decimal
    close_notional_usdt: Decimal
    realized_gross_usdt: Decimal
    fees_usdt: Decimal
    funding_cashflow_usdt: Decimal
    realized_net_usdt: Decimal
    status: str


def reconcile_short(
    *, position_id: str, contract_size: Decimal,
    fills: Iterable[ExecutionFill], funding: Iterable[FundingEvent],
    confirmed_exchange_holding: Decimal,
    fills_complete: bool, funding_complete: bool,
) -> ShortReconciliation:
    """FIFO realized short PnL with immutable IDs and explicit completeness.

    Contract size is base units per contract. Assumes linear USDT-settled
    contracts; inverse/quanto contracts must not use this calculator.
    """
    if not position_id or not fills_complete or not funding_complete:
        raise AccountingHalted("RECONCILIATION_INPUTS_INCOMPLETE")
    size = number(contract_size)
    holding = number(confirmed_exchange_holding, zero_ok=True)
    records = list(fills)
    events = list(funding)
    if not records:
        raise AccountingHalted("NO_CONFIRMED_FILLS")
    trade_ids: set[str] = set()
    funding_ids: set[str] = set()
    lots: list[list[Decimal]] = []  # remaining contracts, opening price
    opened = closed = open_notional = close_notional = gross = fees = Decimal(0)
    for f in records:
        if not f.trade_id or not f.order_id or not f.position_id:
            raise AccountingHalted("MISSING_FILL_IDENTITY")
        if f.trade_id in trade_ids:
            raise AccountingHalted("DUPLICATE_TRADE_ID")
        trade_ids.add(f.trade_id)
        if f.position_id != position_id:
            raise AccountingHalted("POSITION_ID_MISMATCH")
        if f.side not in {"OPEN_SHORT", "CLOSE_SHORT"}:
            raise AccountingHalted("UNSUPPORTED_FILL_SIDE")
        qty, price, fee = number(f.contracts), number(f.price), number(f.fee_usdt, zero_ok=True)
        fees += fee
        if f.side == "OPEN_SHORT":
            opened += qty
            open_notional += qty * price * size
            lots.append([qty, price])
        else:
            closed += qty
            close_notional += qty * price * size
            left = qty
            while left:
                if not lots:
                    raise AccountingHalted("CLOSE_EXCEEDS_PRIOR_OPEN")
                take = min(left, lots[0][0])
                gross += (lots[0][1] - price) * take * size
                lots[0][0] -= take
                left -= take
                if lots[0][0] == 0:
                    lots.pop(0)
    remaining = opened - closed
    if remaining != holding:
        raise AccountingHalted("EXCHANGE_POSITION_QUANTITY_DRIFT")
    funding_sum = Decimal(0)
    for e in events:
        if not e.event_id or not e.position_id:
            raise AccountingHalted("MISSING_FUNDING_IDENTITY")
        if e.event_id in funding_ids:
            raise AccountingHalted("DUPLICATE_FUNDING_EVENT")
        funding_ids.add(e.event_id)
        if e.position_id != position_id:
            raise AccountingHalted("FUNDING_POSITION_MISMATCH")
        funding_sum += signed(e.cashflow_usdt)
    return ShortReconciliation(
        position_id, opened, closed, remaining, open_notional,
        close_notional, gross, fees, funding_sum, gross - fees + funding_sum,
        "CLOSED_RECONCILED" if remaining == 0 else "OPEN_RECONCILED_NOT_CLOSED",
    )

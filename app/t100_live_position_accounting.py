"""Final deterministic accounting from independently verified T100 evidence."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.t100_live_accounting import (
    ExecutionFill,
    FundingEvent,
    ShortReconciliation,
    reconcile_short,
)
from app.t100_live_close_provenance import VerifiedStopCloseProvenance
from app.t100_live_fill_ledger import VerifiedOrderFillSet
from app.t100_live_funding_ledger import VerifiedPositionFundingSet


class PositionAccountingHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedClosedPositionAccounting:
    reconciliation: ShortReconciliation
    entry_order_id: str
    close_order_id: str
    stop_order_id: str
    fill_history_complete: bool = True
    funding_history_complete: bool = True
    close_provenance_verified: bool = True
    live_authorization: bool = False


def reconcile_verified_closed_position(
    *,
    contract_size: Decimal,
    entry_fills: VerifiedOrderFillSet,
    close_provenance: VerifiedStopCloseProvenance,
    close_fills: VerifiedOrderFillSet,
    funding: VerifiedPositionFundingSet,
) -> VerifiedClosedPositionAccounting:
    if (
        not isinstance(contract_size, Decimal)
        or not contract_size.is_finite()
        or contract_size <= 0
        or not isinstance(entry_fills, VerifiedOrderFillSet)
        or not isinstance(close_fills, VerifiedOrderFillSet)
        or not isinstance(close_provenance, VerifiedStopCloseProvenance)
        or not isinstance(funding, VerifiedPositionFundingSet)
    ):
        raise PositionAccountingHalted("POSITION_ACCOUNTING_INPUT_INVALID")

    position_id = entry_fills.position_id
    if (
        entry_fills.side != "OPEN_SHORT"
        or close_fills.side != "CLOSE_SHORT"
        or not entry_fills.strategy_fully_filled_order_complete
        or not close_fills.strategy_fully_filled_order_complete
        or close_fills.position_id != position_id
        or close_provenance.position_id != position_id
        or funding.position_id != position_id
        or close_fills.order_id != close_provenance.close_order_id
        or close_provenance.symbol != entry_fills.symbol
        or close_fills.symbol != entry_fills.symbol
        or not funding.double_collection_finality_verified
        or not funding.economics_verified
    ):
        raise PositionAccountingHalted(
            "POSITION_ACCOUNTING_PROVENANCE_MISMATCH"
        )

    fills: list[ExecutionFill] = []
    for fill in entry_fills.fills + close_fills.fills:
        fills.append(ExecutionFill(
            trade_id=fill.trade_id,
            order_id=fill.order_id,
            position_id=fill.position_id,
            side=fill.side,
            contracts=fill.contracts,
            price=fill.price,
            fee_usdt=fill.fee_usdt,
        ))
    funding_events = [
        FundingEvent(
            event_id=event.event_id,
            position_id=event.position_id,
            cashflow_usdt=event.funding_cashflow_usdt,
        )
        for event in funding.events
    ]

    try:
        reconciled = reconcile_short(
            position_id=position_id,
            contract_size=contract_size,
            fills=fills,
            funding=funding_events,
            confirmed_exchange_holding=Decimal("0"),
            fills_complete=True,
            funding_complete=True,
        )
    except Exception as exc:
        raise PositionAccountingHalted(
            "POSITION_ACCOUNTING_RECONCILIATION_FAILED"
        ) from exc
    if reconciled.status != "CLOSED_RECONCILED":
        raise PositionAccountingHalted(
            "POSITION_ACCOUNTING_NOT_CLOSED"
        )

    return VerifiedClosedPositionAccounting(
        reconciliation=reconciled,
        entry_order_id=entry_fills.order_id,
        close_order_id=close_fills.order_id,
        stop_order_id=close_provenance.stop_order_id,
    )


class PostgresFinalAccountingLedger:
    """Append-only immutable final accounting summary."""

    def __init__(self, pool: Any):
        self.pool = pool

    async def persist(
        self,
        accounting: VerifiedClosedPositionAccounting,
    ) -> str:
        if not isinstance(accounting, VerifiedClosedPositionAccounting):
            raise PositionAccountingHalted("FINAL_ACCOUNTING_INVALID")
        r = accounting.reconciliation
        try:
            row = await self.pool.fetchrow(
                """SELECT entry_order_id,close_order_id,stop_order_id,
                          realized_net_usdt
                   FROM t100_live_final_position_accounting
                   WHERE position_id=$1""",
                r.position_id,
            )
            if row is not None:
                if (
                    str(row["entry_order_id"]) != accounting.entry_order_id
                    or str(row["close_order_id"]) != accounting.close_order_id
                    or str(row["stop_order_id"]) != accounting.stop_order_id
                    or Decimal(str(row["realized_net_usdt"]))
                    != r.realized_net_usdt
                ):
                    raise PositionAccountingHalted(
                        "FINAL_ACCOUNTING_IMMUTABILITY_MISMATCH"
                    )
                return "ALREADY_VERIFIED"
            inserted = await self.pool.fetchval(
                """INSERT INTO t100_live_final_position_accounting
                   (position_id,entry_order_id,close_order_id,stop_order_id,
                    opened_contracts,closed_contracts,open_notional_usdt,
                    close_notional_usdt,realized_gross_usdt,fees_usdt,
                    funding_cashflow_usdt,realized_net_usdt)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                   RETURNING position_id""",
                r.position_id,
                accounting.entry_order_id,
                accounting.close_order_id,
                accounting.stop_order_id,
                r.opened_contracts,
                r.closed_contracts,
                r.open_notional_usdt,
                r.close_notional_usdt,
                r.realized_gross_usdt,
                r.fees_usdt,
                r.funding_cashflow_usdt,
                r.realized_net_usdt,
            )
            if inserted is None:
                raise PositionAccountingHalted(
                    "FINAL_ACCOUNTING_INSERT_FAILED"
                )
            return "PERSISTED"
        except PositionAccountingHalted:
            raise
        except Exception as exc:
            raise PositionAccountingHalted(
                "FINAL_ACCOUNTING_TRANSACTION_FAILED"
            ) from exc

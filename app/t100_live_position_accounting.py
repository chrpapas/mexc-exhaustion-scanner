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
        or entry_fills.final_order_fill_history_complete is not True
        or close_fills.final_order_fill_history_complete is not True
        or entry_fills.strategy_fully_filled_order_complete is not True
        or close_fills.strategy_fully_filled_order_complete is not True
        or entry_fills.fee_currency != "USDT"
        or close_fills.fee_currency != "USDT"
        or entry_fills.order_id == close_fills.order_id
        or close_fills.position_id != position_id
        or close_provenance.position_id != position_id
        or funding.position_id != position_id
        or close_fills.order_id != close_provenance.close_order_id
        or close_provenance.symbol != entry_fills.symbol
        or close_fills.symbol != entry_fills.symbol
        or not close_provenance.stop_order_id
        or close_provenance.live_authorization is not False
        or type(close_provenance.contracts) is not Decimal
        or not close_provenance.contracts.is_finite()
        or close_provenance.contracts <= 0
        or close_provenance.contracts != entry_fills.deal_volume
        or close_provenance.contracts != close_fills.deal_volume
        or funding.double_collection_finality_verified is not True
        or funding.economics_verified is not True
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
    """Immutable, single-transaction final-accounting insert and full readback.

    A lost PostgreSQL response has UNKNOWN commit status: the caller must
    re-observe the exact position ledger, never retry a trade or slot release.
    This writer alone is NOT production settlement authorization. The caller
    must independently establish the at-entry contract multiplier, venue
    fill/funding finality, ownership, leader fence, and closed native stop.
    """

    _FIELDS = (
        "position_id", "entry_order_id", "close_order_id", "stop_order_id",
        "opened_contracts", "closed_contracts",
        "open_notional_usdt", "close_notional_usdt",
        "realized_gross_usdt", "fees_usdt",
        "funding_cashflow_usdt", "realized_net_usdt",
    )
    _NUMERIC = frozenset(_FIELDS[4:])

    def __init__(self, pool: Any):
        if pool is None or not callable(getattr(pool, "acquire", None)):
            raise PositionAccountingHalted("FINAL_ACCOUNTING_RESTRICTED_POOL_REQUIRED")
        self.pool = pool

    @classmethod
    def _expected(cls, accounting: VerifiedClosedPositionAccounting) -> dict[str, Any]:
        if (
            type(accounting) is not VerifiedClosedPositionAccounting
            or type(accounting.reconciliation) is not ShortReconciliation
            or accounting.fill_history_complete is not True
            or accounting.funding_history_complete is not True
            or accounting.close_provenance_verified is not True
            or accounting.live_authorization is not False
        ):
            raise PositionAccountingHalted("FINAL_ACCOUNTING_PROOF_INCOMPLETE")

        r = accounting.reconciliation
        values = dict(
            position_id=r.position_id,
            entry_order_id=accounting.entry_order_id,
            close_order_id=accounting.close_order_id,
            stop_order_id=accounting.stop_order_id,
            opened_contracts=r.opened_contracts,
            closed_contracts=r.closed_contracts,
            open_notional_usdt=r.open_notional_usdt,
            close_notional_usdt=r.close_notional_usdt,
            realized_gross_usdt=r.realized_gross_usdt,
            fees_usdt=r.fees_usdt,
            funding_cashflow_usdt=r.funding_cashflow_usdt,
            realized_net_usdt=r.realized_net_usdt,
        )
        if (
            r.status != "CLOSED_RECONCILED"
            or type(r.remaining_contracts) is not Decimal
            or r.remaining_contracts != 0
            or any(type(values[name]) is not str or not values[name]
                   for name in cls._FIELDS[:4])
            or len({values["entry_order_id"], values["close_order_id"]}) != 2
            or any(type(values[name]) is not Decimal
                   or not values[name].is_finite() for name in cls._NUMERIC)
            or r.opened_contracts <= 0
            or r.closed_contracts != r.opened_contracts
            or r.open_notional_usdt <= 0 or r.close_notional_usdt <= 0
        ):
            raise PositionAccountingHalted("FINAL_ACCOUNTING_EXACT_CLOSED_ECONOMICS_INVALID")
        return values

    @classmethod
    def _same_complete_row(cls, row: Any, expected: dict[str, Any]) -> bool:
        if row is None:
            return False
        try:
            for name in cls._FIELDS:
                value = row[name]
                if name in cls._NUMERIC:
                    if type(value) is bool or Decimal(str(value)) != expected[name]:
                        return False
                elif type(value) is not str or value != expected[name]:
                    return False
            return True
        except (ValueError, ArithmeticError, TypeError, KeyError, IndexError):
            return False

    async def persist(
        self,
        accounting: VerifiedClosedPositionAccounting,
    ) -> str:
        expected = self._expected(accounting)
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction(isolation="read_committed"):
                    # INSERT-only: either a unique exact settlement is inserted
                    # or the already existing immutable row must match ALL
                    # fill, funding, fee and PnL components, not just net PnL.
                    inserted = await conn.fetchval(
                        """INSERT INTO public.t100_live_final_position_accounting
                           (position_id,entry_order_id,close_order_id,stop_order_id,
                            opened_contracts,closed_contracts,open_notional_usdt,
                            close_notional_usdt,realized_gross_usdt,fees_usdt,
                            funding_cashflow_usdt,realized_net_usdt)
                           VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                           ON CONFLICT (position_id) DO NOTHING
                           RETURNING position_id""",
                        *(expected[name] for name in self._FIELDS),
                    )
                    row = await conn.fetchrow(
                        """SELECT position_id,entry_order_id,close_order_id,
                                  stop_order_id,opened_contracts,closed_contracts,
                                  open_notional_usdt,close_notional_usdt,
                                  realized_gross_usdt,fees_usdt,
                                  funding_cashflow_usdt,realized_net_usdt
                           FROM public.t100_live_final_position_accounting
                           WHERE position_id=$1 FOR SHARE""",
                        expected["position_id"],
                    )
                    if not self._same_complete_row(row, expected):
                        raise PositionAccountingHalted(
                            "FINAL_ACCOUNTING_IMMUTABILITY_MISMATCH"
                        )
                    if inserted is None:
                        return "ALREADY_VERIFIED"
                    if inserted != expected["position_id"]:
                        raise PositionAccountingHalted(
                            "FINAL_ACCOUNTING_INSERT_IDENTITY_UNVERIFIED"
                        )
                    return "PERSISTED"
        except PositionAccountingHalted:
            raise
        except Exception:
            # A SQL timeout or lost ACK may follow a COMMIT. No repeat write
            # without an independent immutable-row read and leader review.
            raise PositionAccountingHalted(
                "FINAL_ACCOUNTING_SQL_OUTCOME_UNKNOWN_RECONCILE_ONLY"
            ) from None

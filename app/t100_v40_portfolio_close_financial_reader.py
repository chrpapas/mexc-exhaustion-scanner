"""Read-only V40 four-slot native-close economics after positive venue finality.

Actual source artifacts:
  * original verified filled OPEN_SHORT order and complete deal history;
  * exact independent native CLOSE_SHORT order/deals matching the immutable
    native-stop finality witness;
  * two separately complete MEXC position-scoped funding page collections;
  * current unique public MEXC contract multiplier.
This computes precise realized economics but NEVER writes final accounting,
releases a slot, changes native stops, or submits a new order.

MEXC has returned a conflicting TP/SL trigger label in a prior pilot.
That remains "VENUE_LABEL_CONFLICT": positive actual CLOSE_SHORT fills can
be reconciled economically, but the order must NEVER be relabeled as
confirmed STOP_LOSS just to satisfy an accounting function.

Public contract_size must still be compared to an independently archived
at-entry spec before production settlement release; current contract terms
alone cannot prove historical contract-size continuity.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D,InvalidOperation
from typing import Any

from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_fill_ledger import (
    VerifiedOrderFillSet,build_verified_order_fill_set,
)
from app.t100_live_funding_ledger import (
    VerifiedPositionFundingSet,build_verified_closed_position_funding_set,
)
from app.t100_live_funding_probe import collect_verified_position_funding_pages
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_close_provenance import VerifiedStopCloseProvenance
from app.t100_live_position_accounting import (
    VerifiedClosedPositionAccounting,
    reconcile_verified_closed_position,
)
from app.t100_v40_unreleased_portfolio_native_close import V40PortfolioCloseWitness


class V40CloseFinancialReadHalted(RuntimeError):
    """No venue/DB body or credential embedded in error messages."""


@dataclass(frozen=True)
class V40NativeCloseFinancialEvidence:
    status:str
    accounting:VerifiedClosedPositionAccounting
    entry_fills:VerifiedOrderFillSet
    close_fills:VerifiedOrderFillSet
    funding:VerifiedPositionFundingSet
    close_provenance:VerifiedStopCloseProvenance
    current_contract_size:D
    at_entry_contract_size_independently_verified:bool=False
    historical_contract_multiplier_certified:bool=False
    native_stop_trigger_label_verified:bool=False
    native_trigger_label_conflict:bool=False
    exchange_posts:int=0
    database_writes:int=0
    live_authorization:bool=False


def _positive(v:Any)->D:
    if v is None or type(v) is bool:
        raise V40CloseFinancialReadHalted(
            "V40_CLOSE_FINANCIAL_CONTRACT_NUMBER_INVALID"
        )
    try:
        n=D(str(v))
    except (InvalidOperation,ValueError,TypeError):
        raise V40CloseFinancialReadHalted(
            "V40_CLOSE_FINANCIAL_CONTRACT_NUMBER_INVALID"
        ) from None
    if not n.is_finite() or n<=0:
        raise V40CloseFinancialReadHalted(
            "V40_CLOSE_FINANCIAL_CONTRACT_NUMBER_INVALID"
        )
    return n


def positive_linked_native_close(
    *,witness:V40PortfolioCloseWitness,
    position:PositionCheckpoint,
)->VerifiedStopCloseProvenance:
    """No fabricated STOP_LOSS classification for native TP label conflicts."""
    if (
        type(witness) is not V40PortfolioCloseWitness
        or type(position) is not PositionCheckpoint
        or position.lifecycle_state!="OPEN"
        or witness.status not in (
            "V40_PORTFOLIO_CLOSE_EXCHANGE_TP_LABEL_CONFLICT_UNRELEASED",
            "V40_PORTFOLIO_CLOSE_EXCHANGE_SL_LABEL_OBSERVED_UNRELEASED",
        )
        or witness.closed_position_id!=position.position_id
        or witness.exact_close_order_id is None
        or not witness.exact_close_order_id.isascii()
        or not witness.exact_close_order_id.isdecimal()
        or witness.exact_closed_position_history_verified is not True
        or witness.portfolio_unchanged_across_bookends is not True
        or witness.slot_released is not False
        or witness.live_authorization is not False
        or witness.mexc_posts!=0 or witness.database_writes!=0
        or witness.closing_fills is None
        or type(witness.closing_fills) is not VerifiedOrderFillSet
        or witness.closing_fills.position_id!=position.position_id
        or witness.closing_fills.symbol!=position.symbol
        or witness.closing_fills.side!="CLOSE_SHORT"
        or witness.closing_fills.order_id!=witness.exact_close_order_id
        or witness.closing_fills.deal_volume!=position.contracts
        or witness.closing_fills.final_order_fill_history_complete is not True
        or witness.closing_fills.strategy_fully_filled_order_complete is not True
        or witness.native_terminal is None
        or witness.native_terminal.native_stop_terminal_verified is not True
        or witness.native_terminal.linked_venue_close_filled is not True
        or witness.native_terminal.owned_position_closed_verified is not True
        or (
            witness.status==
            "V40_PORTFOLIO_CLOSE_EXCHANGE_TP_LABEL_CONFLICT_UNRELEASED"
        ) is not witness.native_trigger_label_conflict
    ):
        raise V40CloseFinancialReadHalted(
            "V40_CLOSE_FINANCIAL_POSITIVE_NATIVE_CLOSE_NOT_VERIFIED"
        )
    return VerifiedStopCloseProvenance(
        stop_order_id=position.stop_order_id,
        position_id=position.position_id,
        symbol=position.symbol,
        close_order_id=witness.exact_close_order_id,
        contracts=position.contracts,
        trigger_side=(
            "VENUE_LABEL_CONFLICT"
            if witness.native_trigger_label_conflict else "STOP_LOSS"
        ),
    )


class V40NativeCloseFinancialReadPort:
    def __init__(self,*,probe:ReadOnlyMexcProbe,public_get:Any)->None:
        if (
            type(probe) is not ReadOnlyMexcProbe
            or not callable(getattr(public_get,"get",None))
        ):
            raise V40CloseFinancialReadHalted(
                "V40_CLOSE_FINANCIAL_GET_ONLY_PROBES_REQUIRED"
            )
        self.probe=probe
        self.public_get=public_get

    async def _current_contract_size(self,symbol:str)->D:
        try:
            response=await self.public_get.get(
                "/api/v1/contract/detail",params={"symbol":symbol},
            )
            response.raise_for_status()
            envelope=response.json()
        except Exception:
            raise V40CloseFinancialReadHalted(
                "V40_CLOSE_FINANCIAL_PUBLIC_CONTRACT_GET_FAILED"
            ) from None
        if type(envelope) is not dict or envelope.get("success") is not True:
            raise V40CloseFinancialReadHalted(
                "V40_CLOSE_FINANCIAL_PUBLIC_CONTRACT_UNVERIFIED"
            )
        data=envelope.get("data")
        rows=data if type(data) is list else [data]
        matching=[
            item for item in rows
            if type(item) is dict and item.get("symbol")==symbol
        ]
        if (
            len(matching)!=1
            or matching[0].get("apiAllowed") is not True
        ):
            raise V40CloseFinancialReadHalted(
                "V40_CLOSE_FINANCIAL_CONTRACT_IDENTITY_UNVERIFIED"
            )
        return _positive(matching[0].get("contractSize"))

    async def observe(
        self, *,previous:PositionCheckpoint,
        witness:V40PortfolioCloseWitness,
    )->V40NativeCloseFinancialEvidence:
        # This helper does not grant a production release; it constructs a
        # true financial evidence matrix for eventual independently fenced
        # immutable persistence, and never uses live mutations.
        native=positive_linked_native_close(
            witness=witness,position=previous,
        )
        try:
            order=await self.probe.raw_order_by_id(previous.exchange_order_id)
            entry_deals=await self.probe.raw_order_deal_details(
                previous.exchange_order_id
            )
            entry=build_verified_order_fill_set(
                order_id=previous.exchange_order_id,
                order=order,fills=entry_deals,
                require_strategy_fully_filled=True,
            )
            if (
                entry.position_id!=previous.position_id
                or entry.symbol!=previous.symbol
                or entry.side!="OPEN_SHORT"
                or entry.deal_volume!=previous.contracts
            ):
                raise V40CloseFinancialReadHalted(
                    "V40_CLOSE_FINANCIAL_ORIGINAL_ENTRY_FILL_DRIFT"
                )
            close_order=await self.probe.raw_order_by_id(
                witness.exact_close_order_id
            )
            close_deals=await self.probe.raw_order_deal_details(
                witness.exact_close_order_id
            )
            close=build_verified_order_fill_set(
                order_id=witness.exact_close_order_id,
                order=close_order,fills=close_deals,
                require_strategy_fully_filled=True,
            )
            if close!=witness.closing_fills:
                raise V40CloseFinancialReadHalted(
                    "V40_CLOSE_FINANCIAL_CLOSE_FILL_BOOKENDS_CHANGED"
                )
            first=await collect_verified_position_funding_pages(
                self.probe,position_id=previous.position_id,
            )
            second=await collect_verified_position_funding_pages(
                self.probe,position_id=previous.position_id,
            )
            funding=build_verified_closed_position_funding_set(
                position_id=previous.position_id,first=first,second=second,
            )
            if any(
                event.position_id!=previous.position_id
                or event.symbol!=previous.symbol
                or event.position_type!=2
                for event in funding.events
            ):
                raise V40CloseFinancialReadHalted(
                    "V40_CLOSE_FINANCIAL_FUNDING_POSITION_DRIFT"
                )
            # A funding event outside the exact signed fill interval must
            # not be silently attributed to the closed strategy position.
            opened=min(x.timestamp_ms for x in entry.fills)
            closed=max(x.timestamp_ms for x in close.fills)
            if opened>=closed or any(
                not opened<=e.settle_time_ms<=closed
                for e in funding.events
            ):
                raise V40CloseFinancialReadHalted(
                    "V40_CLOSE_FINANCIAL_FUNDING_OUTSIDE_EXACT_FILL_WINDOW"
                )
            current_contract_size=await self._current_contract_size(
                previous.symbol,
            )
            accounting=reconcile_verified_closed_position(
                contract_size=current_contract_size,
                entry_fills=entry,close_provenance=native,
                close_fills=close,funding=funding,
            )
            if (
                accounting.entry_order_id!=previous.exchange_order_id
                or accounting.close_order_id!=witness.exact_close_order_id
                or accounting.stop_order_id!=previous.stop_order_id
                or accounting.reconciliation.opened_contracts!=previous.contracts
                or accounting.reconciliation.closed_contracts!=previous.contracts
            ):
                raise V40CloseFinancialReadHalted(
                    "V40_CLOSE_FINANCIAL_FINAL_ECONOMICS_IDENTITY_DRIFT"
                )
        except V40CloseFinancialReadHalted:
            raise
        except Exception:
            raise V40CloseFinancialReadHalted(
                "V40_CLOSE_FINANCIAL_SIGNED_FILLS_FUNDING_OR_SIZE_FAILED"
            ) from None
        return V40NativeCloseFinancialEvidence(
            status="V40_NATIVE_CLOSE_ECONOMICS_RECONCILED_CURRENT_MULTIPLIER_UNCERTIFIED",
            accounting=accounting,entry_fills=entry,close_fills=close,
            funding=funding,close_provenance=native,
            current_contract_size=current_contract_size,
            native_stop_trigger_label_verified=(
                native.trigger_side=="STOP_LOSS"
            ),
            native_trigger_label_conflict=(
                native.trigger_side=="VENUE_LABEL_CONFLICT"
            ),
        )

"""UNRELEASED: signed-GET normal native-SL exit reconciliation for V40.

This component cannot submit an order, mutate a stop, mark a checkpoint
CLOSED or produce realized P&L. Normal native stop exits are verified only
from the exact finished TP/SL ID, actual CLOSE_SHORT order/deals, and two
complete globally flat signed account GET bookends. The existing emergency
close pathway is separate and cannot be misidentified as this stop trigger.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.t100_live_close_provenance import (
    VerifiedStopCloseProvenance, build_verified_stop_close_provenance,
    find_finished_stop_by_id, CloseProvenanceHalted,
)
from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_fill_ledger import (
    VerifiedOrderFillSet, build_verified_order_fill_set,
    VerifiedFillLedgerHalted,
)
from app.t100_live_native_stop_execution_audit import (
    assess_native_short_market_stop_row,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


class V40NormalStopObservationHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class V40NormalStopObservation:
    status: str
    stop_provenance: VerifiedStopCloseProvenance | None = None
    closing_fills: VerifiedOrderFillSet | None = None
    signed_account_flat_twice: bool = False
    native_stop_currently_active: bool = False
    exchange_posts: int = 0
    db_writes: int = 0
    entry_allowed: bool = False
    live_authorization: bool = False


async def _frame(probe: ReadOnlyMexcProbe) -> tuple[
    list[dict[str, Any]],list[dict[str, Any]],dict[str,int],int,
]:
    return (
        await probe.raw_open_positions(),
        await probe.raw_open_stops(),
        await probe.raw_open_order_counts(),
        await probe.raw_position_mode(),
    )


def _flat(frame: tuple[Any, ...]) -> bool:
    positions,stops,counts,mode=frame
    return bool(
        type(positions) is list and len(positions)==0
        and type(stops) is list and len(stops)==0
        and type(counts) is dict
        and set(counts)=={
            "sumCount","limitOrderCount","stopOrderCount",
            "planOrderCount","trackOrderCount",
        }
        and all(type(counts[k]) is int and counts[k]==0 for k in counts)
        and mode==1
    )


async def observe_one_normal_v40_native_stop_exit(
    *,
    probe: ReadOnlyMexcProbe,
    checkpoint: PositionCheckpoint,
    wait: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> V40NormalStopObservation:
    """One-shot check. A missing stop never itself proves successful SL."""
    if (
        type(probe) is not ReadOnlyMexcProbe
        or type(checkpoint) is not PositionCheckpoint
        or checkpoint.lifecycle_state != "OPEN"
        or not checkpoint.symbol.endswith("_USDT")
        or checkpoint.contracts <= 0
        or not checkpoint.stop_order_id.isascii()
        or not checkpoint.stop_order_id.isdecimal()
        or not int(checkpoint.stop_order_id)>0
    ):
        raise V40NormalStopObservationHalted("V40_NORMAL_STOP_ORIGIN_INVALID")
    try:
        first=await _frame(probe)
        await wait(.3)
        second=await _frame(probe)
    except Exception:
        return V40NormalStopObservation("V40_NORMAL_STOP_SIGNED_GET_INCOMPLETE_HALT")

    if first!=second:
        return V40NormalStopObservation("V40_NORMAL_STOP_ACCOUNT_BOOKENDS_DRIFT_HALT")
    if not _flat(first):
        positions,stops,counts,mode=first
        if (
            type(positions) is list and len(positions)==1
            and type(stops) is list and len(stops)==1
            and type(counts) is dict
            and counts.get("sumCount")==counts.get("stopOrderCount")==1
            and counts.get("limitOrderCount")==0
            and counts.get("planOrderCount")==0
            and counts.get("trackOrderCount")==0
            and mode==1
            and str(positions[0].get("positionId") or "")==checkpoint.position_id
            and positions[0].get("symbol")==checkpoint.symbol
            and positions[0].get("positionType")==2
            and positions[0].get("openType")==2
            and positions[0].get("leverage")==1
            and positions[0].get("state")==1
            and str(positions[0].get("holdVol"))==str(checkpoint.contracts)
            and str(stops[0].get("id") or "")==checkpoint.stop_order_id
            and assess_native_short_market_stop_row(
                stop=stops[0],symbol=checkpoint.symbol,
                position_id=checkpoint.position_id,
                position_quantity=checkpoint.contracts,
                maximum_short_stop_price=checkpoint.stop_price,
                verified_filled_entry_order_id=checkpoint.exchange_order_id,
            ).active_native_full_volume_market_sl_observed is True
        ):
            return V40NormalStopObservation(
                "V40_NORMAL_STOP_STILL_OPEN_AND_PROTECTED",
                native_stop_currently_active=True,
            )
        return V40NormalStopObservation(
            "V40_NORMAL_STOP_NONFLAT_OR_UNPROTECTED_GET_HALT"
        )

    # Only actual globally flat account and no pending orders may proceed
    # to finished-stop/close-order *positive* evidence. Flat alone is not exit.
    try:
        finished=await find_finished_stop_by_id(
            probe,stop_order_id=checkpoint.stop_order_id,
            max_pages=4,
        )
        provenance=build_verified_stop_close_provenance(
            finished_stop=finished,
            expected_stop_order_id=checkpoint.stop_order_id,
            expected_position_id=checkpoint.position_id,
            expected_symbol=checkpoint.symbol,
            expected_contracts=checkpoint.contracts,
        )
        close=await probe.raw_order_by_id(provenance.close_order_id)
        deals=await probe.raw_order_deal_details(provenance.close_order_id)
        closing=build_verified_order_fill_set(
            order_id=provenance.close_order_id,
            order=close,fills=deals,require_strategy_fully_filled=True,
        )
        if not (
            closing.symbol==checkpoint.symbol
            and closing.position_id==checkpoint.position_id
            and closing.side=="CLOSE_SHORT"
            and closing.deal_volume==checkpoint.contracts
            and closing.final_order_fill_history_complete
            and closing.strategy_fully_filled_order_complete
        ):
            raise V40NormalStopObservationHalted(
                "V40_NORMAL_STOP_CLOSING_ORDER_OR_DEAL_MISMATCH"
            )
        # A second globally flat account pair after the potentially slow
        # terminal history pagination is needed: no stale pre-history proof.
        end_first=await _frame(probe)
        await wait(.3)
        end_second=await _frame(probe)
        if end_first!=end_second or not _flat(end_first):
            return V40NormalStopObservation(
                "V40_NORMAL_STOP_POST_HISTORY_FLAT_GET_UNVERIFIED_HALT"
            )
    except Exception:
        return V40NormalStopObservation(
            "V40_NORMAL_STOP_HISTORY_OR_FILLS_UNVERIFIED_HALT"
        )
    return V40NormalStopObservation(
        "V40_NORMAL_STOP_EXCHANGE_AND_FLAT_GET_VERIFIED_NO_DB_WRITE",
        stop_provenance=provenance,closing_fills=closing,
        signed_account_flat_twice=True,
    )

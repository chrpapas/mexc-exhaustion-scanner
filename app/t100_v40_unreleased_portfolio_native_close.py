"""UNRELEASED: positive native-stop close evidence for one V40 slot while 0–3 remain.

The existing one-shot close observer requires a globally FLAT account; this
is incompatible with the frozen four-slot V40 portfolio. Here every signed
account bookend must show *exactly* the expected surviving owned CROSS
SHORT positions with active full-volume native MARKET SLs, zero unrelated
orders, and positive account-wide inventory completeness. A missing closed
position is NEVER enough to declare closure.

Terminal evidence must additionally establish exact finished native TP/SL,
filled exchange-generated CLOSE_SHORT, complete order-scoped deal history
and two positive terminal position-history/ID GET observations. MEXC's known
TP/SL triggerSide conflict is preserved as an explicit uncertainty; it must
not be mislabeled as a proven STOP LOSS.

NO exchange POST, DB mutation, slot release, trading authorization, PnL
certification, or normal-stop trigger assertion occurs in this component.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal as D, InvalidOperation
from typing import Any, Awaitable, Callable, Sequence

from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_fill_ledger import (
    VerifiedOrderFillSet, build_verified_order_fill_set,
)
from app.t100_live_four_slot_snapshot_coordinator import _venue_digest
from app.t100_live_inventory_completeness_probe import (
    run_current_inventory_completeness_probe,
)
from app.t100_live_native_stop_close_recovery import (
    NativeStopRecoveryReport, recover_native_stop_close_read_only,
)
from app.t100_live_native_stop_execution_audit import (
    assess_native_short_market_stop_row,
)
from app.t100_live_owned_emergency_guardian import OwnedShort, _validate_owned
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_close_provenance import find_finished_stop_by_id


class V40PortfolioCloseHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class V40PortfolioCloseWitness:
    status: str
    closed_position_id: str | None = None
    exact_close_order_id: str | None = None
    surviving_protected_slots: int = 0
    closing_fills: VerifiedOrderFillSet | None = None
    native_terminal: NativeStopRecoveryReport | None = None
    exact_closed_position_history_verified: bool = False
    original_native_stop_fill_label_proven: bool = False
    native_trigger_label_conflict: bool = False
    portfolio_unchanged_across_bookends: bool = False
    live_authorization: bool = False
    slot_released: bool = False
    mexc_posts: int = 0
    database_writes: int = 0


@dataclass(frozen=True)
class _CriticalFrame:
    positions: list[dict[str, Any]]
    stops: list[dict[str, Any]]
    account_digest: str
    order_counts: tuple[int,int,int,int,int]


_ORDER_KEYS=(
    "sumCount","stopOrderCount","limitOrderCount",
    "planOrderCount","trackOrderCount",
)


def _verified_survivor_snapshot(
    *,
    positions: Any,
    stops: Any,
    order_counts: Any,
    hedge_mode: Any,
    certificate: Any,
    surviving: Sequence[PositionCheckpoint],
    finished: OwnedShort,
)->_CriticalFrame:
    """Require exact account topology and every other slot's full native SL.

    Certificate is produced by the independent complete-inventory GET
    collector and must match these same position/stop critical fields.
    """
    def invalid(code:str):
        raise V40PortfolioCloseHalted(code)
    if (
        type(positions) is not list or type(stops) is not list
        or type(order_counts) is not dict
        or any(type(row) is not dict for row in positions+stops)
        or type(hedge_mode) is not int or hedge_mode!=1
        or type(certificate) is not dict
        or type(surviving) not in (tuple,list)
        or len(surviving)>3
        or any(type(x) is not PositionCheckpoint for x in surviving)
        or type(finished) is not OwnedShort
    ):
        invalid("V40_PORTFOLIO_CLOSE_SIGNED_ACCOUNT_SHAPE_INVALID")
    if not (
        set(order_counts)==set(_ORDER_KEYS)
        and all(type(order_counts[k]) is int and order_counts[k]>=0
                for k in _ORDER_KEYS)
        and order_counts["sumCount"]==order_counts["stopOrderCount"]
        ==len(surviving)
        and all(order_counts[k]==0 for k in
                ("limitOrderCount","planOrderCount","trackOrderCount"))
        and len(positions)==len(stops)==len(surviving)
    ):
        invalid("V40_PORTFOLIO_CLOSE_UNRELATED_EXPOSURE_OR_ORDERS")
    try:
        fingerprint=_venue_digest(positions,stops)
    except Exception:
        invalid("V40_PORTFOLIO_CLOSE_ACCOUNT_DIGEST_INVALID")
    if (
        certificate.get("account_wide_completeness_verified") is not True
        or certificate.get("critical_inventory_equal_across_witness_window")
        is not True
        or certificate.get("critical_inventory_sha256")!=fingerprint
        or certificate.get("before_positions")!=len(surviving)
        or certificate.get("after_positions")!=len(surviving)
        or certificate.get("before_current_tpsl")!=len(surviving)
        or certificate.get("after_current_tpsl")!=len(surviving)
        or certificate.get("exchange_atomicity_verified") is not False
        or certificate.get("live_authorization") is not False
        or certificate.get("entry_allowed") is not False
    ):
        invalid("V40_PORTFOLIO_CLOSE_ACCOUNT_COMPLETENESS_UNVERIFIED")
    survivor_ids={cp.position_id for cp in surviving}
    stop_ids={cp.stop_order_id for cp in surviving}
    symbols={cp.symbol for cp in surviving}
    if (
        len(survivor_ids)!=len(surviving)
        or len(stop_ids)!=len(surviving)
        or len(symbols)!=len(surviving)
        or finished.position_id in survivor_ids
        or finished.symbol in symbols
        or finished.stop_order_id in stop_ids
    ):
        invalid("V40_PORTFOLIO_CLOSE_SURVIVOR_IDENTITIES_INVALID")
    by_pos={str(row.get("positionId") or ""):row for row in positions}
    by_stop={str(row.get("positionId") or ""):row for row in stops}
    if (
        set(by_pos)!=survivor_ids or set(by_stop)!=survivor_ids
        or len(by_pos)!=len(surviving) or len(by_stop)!=len(surviving)
    ):
        invalid("V40_PORTFOLIO_CLOSE_UNOWNED_POSITION_OR_STOP")
    for cp in surviving:
        pos=by_pos[cp.position_id]
        stop=by_stop[cp.position_id]
        try:
            contracts=D(str(pos.get("holdVol")))
        except (TypeError,ValueError,InvalidOperation):
            invalid("V40_PORTFOLIO_CLOSE_SURVIVOR_VOLUME_INVALID")
        if not (
            cp.lifecycle_state=="OPEN"
            and cp.symbol==pos.get("symbol")
            and contracts.is_finite() and contracts==cp.contracts
            and type(pos.get("positionType")) is int
            and pos.get("positionType")==2
            and type(pos.get("state")) is int and pos.get("state")==1
            and type(pos.get("openType")) is int and pos.get("openType")==2
            and type(pos.get("leverage")) is int
            and 1<=pos.get("leverage")<=125
            and str(stop.get("id") or stop.get("stopPlanOrderId"))
                ==cp.stop_order_id
            and assess_native_short_market_stop_row(
                stop=stop,symbol=cp.symbol,
                position_id=cp.position_id,
                position_quantity=cp.contracts,
                maximum_short_stop_price=cp.stop_price,
                verified_filled_entry_order_id=cp.exchange_order_id,
            ).active_native_full_volume_market_sl_observed is True
        ):
            invalid("V40_PORTFOLIO_CLOSE_SURVIVING_SHORT_UNPROTECTED")
    return _CriticalFrame(
        positions,stops,fingerprint,
        tuple(order_counts[k] for k in _ORDER_KEYS),
    )


async def _certified_frame(
    probe: ReadOnlyMexcProbe,
    *,
    surviving:Sequence[PositionCheckpoint],
    finished:OwnedShort,
)->_CriticalFrame:
    positions=await probe.raw_open_positions()
    stops=await probe.raw_open_stops()
    counts=await probe.raw_open_order_counts()
    mode=await probe.raw_position_mode()
    certificate=await run_current_inventory_completeness_probe(probe)
    return _verified_survivor_snapshot(
        positions=positions,stops=stops,order_counts=counts,
        hedge_mode=mode,certificate=certificate,
        surviving=surviving,finished=finished,
    )


def _same_frames(a:_CriticalFrame,b:_CriticalFrame)->bool:
    return (
        type(a) is _CriticalFrame and type(b) is _CriticalFrame
        and a.account_digest==b.account_digest
        and a.order_counts==b.order_counts
    )


async def observe_one_native_exit_among_v40_survivors(
    *,
    probe:ReadOnlyMexcProbe,
    closing:OwnedShort,
    account_scope_sha256:str,
    surviving:Sequence[PositionCheckpoint],
    wait:Callable[[float],Awaitable[Any]]=asyncio.sleep,
)->V40PortfolioCloseWitness:
    """Positive one-position finality; never grant a portfolio slot release."""
    halt=lambda reason:V40PortfolioCloseWitness(status=reason)
    if type(probe) is not ReadOnlyMexcProbe:
        raise V40PortfolioCloseHalted("V40_PORTFOLIO_CLOSE_READONLY_PROBE_REQUIRED")
    try:
        _validate_owned(closing,account_scope_sha256)
        if (
            type(surviving) not in (tuple,list)
            or len(surviving)>3
            or any(type(cp) is not PositionCheckpoint for cp in surviving)
        ):
            raise V40PortfolioCloseHalted(
                "V40_PORTFOLIO_CLOSE_RECOVERY_ORIGIN_INVALID"
            )
        first=await _certified_frame(
            probe,surviving=surviving,finished=closing,
        )
        await wait(.3)
        second=await _certified_frame(
            probe,surviving=surviving,finished=closing,
        )
        if not _same_frames(first,second):
            return halt("V40_PORTFOLIO_CLOSE_INITIAL_BOOKENDS_DRIFT")
    except Exception:
        return halt("V40_PORTFOLIO_CLOSE_INITIAL_SIGNED_GET_UNVERIFIED")
    # The *actual* closed position has to be found twice in terminal
    # history with an exact signed GET-by-positionId negative bookend.
    try:
        result=await recover_native_stop_close_read_only(
            owned=closing,actual_scope=account_scope_sha256,probe=probe,
        )
        if not (
            type(result) is NativeStopRecoveryReport
            and result.entry_filled_verified is True
            and result.native_stop_terminal_verified is True
            and result.linked_venue_close_filled is True
            and result.owned_position_closed_verified is True
            and result.stop_loss_trigger_side_proven is False
        ):
            return halt("V40_PORTFOLIO_CLOSE_TERMINAL_OR_HISTORY_NOT_PROVEN")
        finished=await find_finished_stop_by_id(
            probe,stop_order_id=closing.stop_order_id,max_pages=4,
        )
        close_id=str(finished.get("placeOrderId") or "")
        if (
            not close_id.isascii() or not close_id.isdecimal()
            or int(close_id)<=0
            or str(finished.get("positionId") or "")!=closing.position_id
            or finished.get("symbol")!=closing.symbol
            or str(finished.get("id") or finished.get("stopPlanOrderId"))
                !=closing.stop_order_id
            or finished.get("triggerSide") not in (1,2)
            or (finished["triggerSide"]==1)!=result.venue_trigger_side_conflict
        ):
            return halt("V40_PORTFOLIO_CLOSE_STOP_AND_HISTORY_DISAGREE")
        order=await probe.raw_order_by_id(close_id)
        deals=await probe.raw_order_deal_details(close_id)
        fills=build_verified_order_fill_set(
            order_id=close_id,order=order,fills=deals,
            require_strategy_fully_filled=True,
        )
        if not (
            fills.final_order_fill_history_complete
            and fills.strategy_fully_filled_order_complete
            and fills.side=="CLOSE_SHORT"
            and fills.position_id==closing.position_id
            and fills.symbol==closing.symbol
            and fills.deal_volume==closing.contracts
        ):
            return halt("V40_PORTFOLIO_CLOSE_EXACT_DEALS_NOT_VERIFIED")
        end_first=await _certified_frame(
            probe,surviving=surviving,finished=closing,
        )
        await wait(.3)
        end_second=await _certified_frame(
            probe,surviving=surviving,finished=closing,
        )
        if (
            not _same_frames(end_first,end_second)
            or not _same_frames(first,end_first)
        ):
            return halt("V40_PORTFOLIO_CLOSE_SURVIVORS_CHANGED_DURING_HISTORY")
    except Exception:
        return halt("V40_PORTFOLIO_CLOSE_POSITIVE_HISTORY_OR_DEALS_UNVERIFIED")
    return V40PortfolioCloseWitness(
        status=(
            "V40_PORTFOLIO_CLOSE_EXCHANGE_TP_LABEL_CONFLICT_UNRELEASED"
            if result.venue_trigger_side_conflict else
            "V40_PORTFOLIO_CLOSE_EXCHANGE_SL_LABEL_OBSERVED_UNRELEASED"
        ),
        closed_position_id=closing.position_id,
        exact_close_order_id=close_id,
        surviving_protected_slots=len(surviving),
        closing_fills=fills,native_terminal=result,
        exact_closed_position_history_verified=True,
        native_trigger_label_conflict=result.venue_trigger_side_conflict,
        portfolio_unchanged_across_bookends=True,
    )

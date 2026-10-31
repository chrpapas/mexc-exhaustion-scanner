"""Non-authorizing four-slot V40 admission plan over independently supplied evidence.

This module makes NO API calls, DB reads/writes, leases or exchange mutations.
It cannot issue a live permit. Live code must independently refresh inventory,
prove full durable ownership, acquire/fence the leader and enforce real margin,
book liquidity and risk immediately before any possible order.

This planner deliberately uses the FROZEN single-candle V40 admission policy,
not the prototype H4 entry-campaign VOL30 planner.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from app.t100_live_account_reconciliation import assess_account_reconciliation
from app.t100_live_durable_recovery import DurableRecoveryReport
from app.t100_live_supervisor import ManagedShort
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult, _venue_digest, _valid_counts,
)
from app.t100_v40_admission import PriorBar, V40Admission, admission

MAX_SLOTS = 4


@dataclass(frozen=True)
class FourSlotPlan:
    action: str
    reason: str
    occupied_slots: int = 0
    modeled_notional_usdt: float = 0.0
    desired_notional_usdt: float = 0.0
    v40_admission: V40Admission | None = None
    candidate_symbol: str | None = None
    # These two flags are intentionally never made true by a planning result.
    entry_allowed: bool = False
    live_authorization: bool = False


def _count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _verified_slot_symbols(
    inventory: Any,
    recovery: Any,
) -> tuple[tuple[str, ...], str]:
    """Require complete stable account plus real non-flat recovery result.

    A caller-supplied boolean, integer position count or manually labelled
    'managed' row is NOT accepted as T100 ownership evidence. The report is
    still a *read-only model input*, not a cryptographically trusted permit.
    """
    if not isinstance(inventory, dict):
        return (), "INVENTORY_EVIDENCE_MISSING"
    if (
        inventory.get("account_wide_completeness_verified") is not True
        or inventory.get("critical_inventory_equal_across_witness_window")
        is not True
    ):
        return (), "ACCOUNT_INVENTORY_NOT_CERTIFIED_STABLE"

    positions = inventory.get("after_positions")
    stops = inventory.get("after_current_tpsl")
    counts = inventory.get("open_order_counts")
    if not _count(positions) or not _count(stops) or not isinstance(counts, dict):
        return (), "INVENTORY_COUNTS_INVALID"

    names = (
        "sumCount", "limitOrderCount", "stopOrderCount",
        "planOrderCount", "trackOrderCount",
    )
    if any(not _count(counts.get(k)) for k in names):
        return (), "ORDER_COUNTS_INVALID"
    if counts["sumCount"] != sum(counts[k] for k in names[1:]):
        return (), "ORDER_COUNTS_INCOHERENT"
    if positions > MAX_SLOTS:
        return (), "ACCOUNT_EXCEEDS_FOUR_SLOTS"
    if stops != positions or counts["stopOrderCount"] != stops:
        return (), "POSITION_STOP_COUNT_MISMATCH"
    if any(counts[k] for k in ("limitOrderCount", "planOrderCount", "trackOrderCount")):
        return (), "UNRELATED_INFLIGHT_ORDERS_PRESENT"

    if positions == 0:
        if recovery is not None:
            return (), "FLAT_ACCOUNT_WITH_NONFLAT_RECOVERY_REPORT"
        account = assess_account_reconciliation(inventory)
        if account.get("flat_account_reconciled") is not True:
            return (), "FLAT_ACCOUNT_RECONCILIATION_FAILED"
        return (), "VERIFIED_FLAT_ACCOUNT"

    if not isinstance(recovery, DurableRecoveryReport):
        return (), "NONFLAT_DURABLE_RECOVERY_REQUIRED"
    if (
        recovery.mode != "SHADOW_ONLY_RECOVERED_NONFLAT"
        or recovery.live_authorization is not False
        or not isinstance(recovery.recovered, tuple)
        or len(recovery.recovered) != positions
        or any(type(x) is not ManagedShort for x in recovery.recovered)
        or any(type(getattr(recovery, x)) is not int for x in (
            "checkpoint_count", "exchange_position_count", "exchange_stop_count"
        ))
        or recovery.checkpoint_count != positions
        or recovery.exchange_position_count != positions
        or recovery.exchange_stop_count != stops
    ):
        return (), "NONFLAT_RECOVERY_REPORT_MISMATCH"

    symbols = tuple(p.symbol for p in recovery.recovered)
    positions_ids = tuple(p.position_id for p in recovery.recovered)
    stops_ids = tuple(p.stop_order_id for p in recovery.recovered)
    if (
        any(not isinstance(s, str) or not s for s in symbols)
        or any(not isinstance(x, str) or not x for x in positions_ids + stops_ids)
        or len(set(symbols)) != positions
        or len(set(positions_ids)) != positions
        or len(set(stops_ids)) != positions
    ):
        return (), "DUPLICATE_OR_INVALID_MANAGED_IDENTITY"

    account = assess_account_reconciliation(
        inventory, durable_nonflat_recovery_verified=True,
    )
    if account.get("managed_nonflat_account_reconciled") is not True:
        return (), "MANAGED_ACCOUNT_RECONCILIATION_FAILED"
    return symbols, "VERIFIED_MANAGED_ACCOUNT_MODEL"


def plan_four_slot_v40(
    *,
    inventory: Any,
    durable_recovery: Any,
    symbol: str,
    prior_bars: Sequence[PriorBar],
    admission_open_ts: int,
    stage1_atr7_pct: float,
    equity_usdt: float,
    marked_existing_usdt: float,
    used_same_bar_usdt: float = 0.0,
    cost_bps: float = 100.0,
) -> FourSlotPlan:
    """Model-only V40 decision. ADMIT is NEVER permission to submit an order.

    Caller must provide independent, fresh MEXC and durable evidence; this
    function does not independently validate snapshot age or serializability.
    """
    symbols, state = _verified_slot_symbols(inventory, durable_recovery)
    if state not in {"VERIFIED_FLAT_ACCOUNT", "VERIFIED_MANAGED_ACCOUNT_MODEL"}:
        return FourSlotPlan("BLOCK", state)
    return _plan_v40_for_verified_account_symbols(
        symbols=symbols,
        symbol=symbol, prior_bars=prior_bars,
        admission_open_ts=admission_open_ts,
        stage1_atr7_pct=stage1_atr7_pct,
        equity_usdt=equity_usdt,
        marked_existing_usdt=marked_existing_usdt,
        used_same_bar_usdt=used_same_bar_usdt,
        cost_bps=cost_bps,
    )


def _plan_v40_for_verified_account_symbols(
    *,
    symbols: tuple[str, ...],
    symbol: str,
    prior_bars: Sequence[PriorBar],
    admission_open_ts: int,
    stage1_atr7_pct: float,
    equity_usdt: float,
    marked_existing_usdt: float,
    used_same_bar_usdt: float = 0.0,
    cost_bps: float = 100.0,
) -> FourSlotPlan:
    """Shared frozen single-candle V40 policy. Does NOT permit exchange orders."""
    occupied = len(symbols)
    if not isinstance(symbol, str) or re.fullmatch(r"[A-Z0-9]+_USDT", symbol) is None:
        return FourSlotPlan("BLOCK", "INVALID_CANDIDATE_SYMBOL", occupied)
    if symbol in {"BTC_USDT", "ETH_USDT"}:
        return FourSlotPlan("BLOCK", "EXCLUDED_CANDIDATE_SYMBOL", occupied)
    if occupied >= MAX_SLOTS:
        return FourSlotPlan("REJECT", "four_slot_limit", occupied)
    if symbol in symbols:
        return FourSlotPlan("REJECT", "duplicate_symbol", occupied)
    if type(admission_open_ts) is not int or type(prior_bars) not in (tuple, list):
        return FourSlotPlan("BLOCK", "INVALID_CANDIDATE_HISTORY", occupied)
    if any(type(bar) is not PriorBar for bar in prior_bars):
        return FourSlotPlan("BLOCK", "INVALID_CANDIDATE_HISTORY", occupied)

    try:
        modeled = admission(
            prior_bars=prior_bars,
            admission_open_ts=admission_open_ts,
            equity_usdt=equity_usdt,
            marked_existing_usdt=marked_existing_usdt,
            stage1_atr7_pct=stage1_atr7_pct,
            active_positions=occupied,
            already_open_symbol=False,
            used_same_bar_usdt=used_same_bar_usdt,
            cost_bps=cost_bps,
        )
    except (TypeError, ValueError, OverflowError):
        return FourSlotPlan("BLOCK", "INVALID_FROZEN_V40_INPUTS", occupied)

    return FourSlotPlan(
        action=modeled.action,
        reason=modeled.reason,
        occupied_slots=occupied,
        modeled_notional_usdt=modeled.modeled_notional_usdt,
        desired_notional_usdt=modeled.desired_notional_usdt,
        v40_admission=modeled,
        candidate_symbol=symbol,
    )


def plan_shared_four_slot_v40(
    *,
    snapshot: FourSlotSnapshotResult,
    account_positions: Any,
    account_stops: Any,
    symbol: str,
    prior_bars: Sequence[PriorBar],
    admission_open_ts: int,
    stage1_atr7_pct: float,
    equity_usdt: float,
    marked_existing_usdt: float,
    used_same_bar_usdt: float = 0.0,
    cost_bps: float = 100.0,
) -> FourSlotPlan:
    """Non-authorizing V40 *model* for mixed T100 + manually opened positions.

    The snapshot MUST come from the GET/SELECT-only shared-account coordinator.
    Its complete-account critical inventory hash must match the provided
    exact raw rows. All four account slots count. Only T100-owned positions
    require native SLs and durable recovery. Unknown/uncheckpointed positions
    remain explicitly unverified as operator-owned: this is NOT evidence for
    issuing a live mutation permit or deriving shared wallet collateral.

    Equity and marked existing exposure are hypothetical planner inputs,
    NOT derived from this snapshot. A live executor must independently prove
    them, position provenance, current tiers, order intent state, capacity,
    account-wide cross risk, and stops immediately before any action.
    """
    def blocked(reason: str) -> FourSlotPlan:
        return FourSlotPlan("BLOCK", reason)

    if type(snapshot) is not FourSlotSnapshotResult:
        return blocked("SHARED_ACCOUNT_COORDINATOR_WITNESS_REQUIRED")
    if snapshot.status not in {
        "SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING",
        "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
    }:
        return blocked("SHARED_ACCOUNT_SNAPSHOT_STATUS_UNVERIFIED")
    if (
        not isinstance(snapshot.ledger_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", snapshot.ledger_hash)
        or not isinstance(snapshot.critical_inventory_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", snapshot.critical_inventory_hash)
        or snapshot.entry_allowed is not False
        or snapshot.live_authorization is not False
        or type(snapshot.account_positions_observed) is not int
        or type(snapshot.manual_positions_observed) is not int
        or type(snapshot.occupied_slots) is not int
    ):
        return blocked("SHARED_ACCOUNT_SNAPSHOT_EVIDENCE_INCOMPLETE")
    total = snapshot.account_positions_observed
    manual = snapshot.manual_positions_observed
    owned = snapshot.occupied_slots
    if (
        not 0 <= owned <= MAX_SLOTS
        or not 0 <= manual <= MAX_SLOTS
        or not 0 <= total <= MAX_SLOTS
        or owned + manual != total
        or (snapshot.status == "SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING"
            and (owned != 0 or snapshot.recovery is not None))
        or (snapshot.status == "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING"
            and (owned == 0 or type(snapshot.recovery) is not DurableRecoveryReport))
    ):
        return blocked("SHARED_ACCOUNT_SLOT_COUNTS_UNVERIFIED")
    if (
        type(account_positions) is not list
        or type(account_stops) is not list
        or len(account_positions) != total
        or any(type(x) is not dict for x in account_positions + account_stops)
        or not _valid_counts(snapshot.inventory, total, len(account_stops))
    ):
        return blocked("SHARED_ACCOUNT_INVENTORY_COUNTS_UNVERIFIED")
    try:
        if _venue_digest(account_positions, account_stops) != snapshot.critical_inventory_hash:
            return blocked("SHARED_ACCOUNT_INVENTORY_HASH_DRIFT")
    except (TypeError, ValueError, OverflowError):
        return blocked("SHARED_ACCOUNT_INVENTORY_HASH_UNVERIFIED")

    position_ids: set[str] = set()
    all_symbols: set[str] = set()
    by_position: dict[str, dict[str, Any]] = {}
    for row in account_positions:
        pid = row.get("positionId") or row.get("id")
        sym = row.get("symbol")
        if (
            pid is None or type(pid) is bool or not str(pid).strip()
            or str(pid).strip() in {"0", "None"}
            or type(sym) is not str
            or re.fullmatch(r"[A-Z0-9]+_USDT", sym) is None
            or type(row.get("positionType")) is not int
            or row["positionType"] not in {1, 2}
            or type(row.get("openType")) is not int
            or row["openType"] not in {1, 2}
            or str(pid) in position_ids
            or sym in all_symbols
        ):
            return blocked("SHARED_ACCOUNT_POSITION_IDENTITY_UNVERIFIED")
        position_ids.add(str(pid))
        all_symbols.add(sym)
        by_position[str(pid)] = row
    stops_seen: set[str] = set()
    stops_by_position: dict[str, list[dict[str, Any]]] = {}
    for row in account_stops:
        sid = row.get("id") or row.get("stopPlanOrderId")
        pid = row.get("positionId")
        if (
            sid is None or type(sid) is bool or not str(sid).strip()
            or pid is None or type(pid) is bool
            or str(sid) in stops_seen or str(pid) not in by_position
            or (row.get("symbol") is not None
                and row["symbol"] != by_position[str(pid)]["symbol"])
        ):
            return blocked("SHARED_ACCOUNT_STOP_IDENTITY_UNVERIFIED")
        stops_seen.add(str(sid))
        stops_by_position.setdefault(str(pid), []).append(row)
    recovered = snapshot.recovery
    if owned:
        if (
            type(recovered) is not DurableRecoveryReport
            or recovered.mode != "SHADOW_ONLY_RECOVERED_NONFLAT"
            or recovered.live_authorization is not False
            or type(recovered.recovered) is not tuple
            or len(recovered.recovered) != owned
            or recovered.checkpoint_count != owned
            or recovered.exchange_position_count != owned
            or recovered.exchange_stop_count != owned
            or any(type(r) is not ManagedShort for r in recovered.recovered)
        ):
            return blocked("SHARED_ACCOUNT_T100_RECOVERY_UNVERIFIED")
        matched: set[str] = set()
        for r in recovered.recovered:
            position = by_position.get(r.position_id)
            native_stops = stops_by_position.get(r.position_id, [])
            if (
                r.position_id in matched
                or position is None or position["symbol"] != r.symbol
                or position["positionType"] != 2 or position["openType"] != 2
                or len(native_stops) != 1
                or str(native_stops[0].get("id") or native_stops[0].get("stopPlanOrderId")) != r.stop_order_id
            ):
                return blocked("SHARED_ACCOUNT_T100_PROTECTION_OR_MODE_UNVERIFIED")
            matched.add(r.position_id)
        if len(matched) != owned:
            return blocked("SHARED_ACCOUNT_T100_IDENTITY_UNVERIFIED")

    # Never use owned count as active_positions. All manual positions occupy
    # slots; even a manual LONG on the candidate symbol blocks entry.
    # This does not certify collateral, mark-to-market exposure or liquidity.
    return _plan_v40_for_verified_account_symbols(
        symbols=tuple(sorted(all_symbols)),
        symbol=symbol,
        prior_bars=prior_bars, admission_open_ts=admission_open_ts,
        stage1_atr7_pct=stage1_atr7_pct, equity_usdt=equity_usdt,
        marked_existing_usdt=marked_existing_usdt,
        used_same_bar_usdt=used_same_bar_usdt, cost_bps=cost_bps,
    )

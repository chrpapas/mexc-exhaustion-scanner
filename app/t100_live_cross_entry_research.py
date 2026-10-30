"""Pure offline cross-margin order-contract research; NEVER an order adapter.

This module is intentionally disconnected from the MEXC transport,
LiveMutationPermit, service runtime and DB. It cannot POST, sign, place stops
or grant permission. An EntryIntent may be transformed into a hypothetical
MEXC create-order payload ONLY for offline review. It does not validate that
MEXC will honor attached TP/SL, that any fill is protected, or that an
ISOLATED current account configuration will accept CROSS entry.

Important: the existing T100 production/pilot transport supports isolated
1x only. This module MUST NOT be imported by a live execution path.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping

from app.t100_live_execution import EntryIntent

D = Decimal
_STOP = D("1.75")
_MAX_RESEARCH_LEVERAGE = 2  # existing controls bound; not venue certification

@dataclass(frozen=True)
class CrossEntryOfflineReview:
    status: str
    hypothetical_payload_shape_valid: bool = False
    current_inventory_consistent: bool = False
    current_account_flat_and_order_free: bool = False
    private_current_cross_observed: bool = False
    candidate_tier_currently_compared: bool = False
    blockers: tuple[str, ...] = ()
    venue_order_acceptance_verified: bool = False
    stop_attached_atomically_verified: bool = False
    native_stop_protectiveness_verified: bool = False
    stressed_tiers_verified: bool = False
    full_account_collateral_verified: bool = False
    cross_margin_adapter_implemented: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _positive(value: Any) -> D:
    if value is None or type(value) is bool:
        raise ValueError("INVALID_NUMBER")
    try:
        n = D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("INVALID_NUMBER") from None
    if not n.is_finite() or n <= 0:
        raise ValueError("INVALID_NUMBER")
    return n


def _hypothetical_payload(
    intent: EntryIntent, *, contract_size: Any,
    volume_step: Any, price_tick: Any, leverage: int,
) -> dict[str, Any]:
    """Validate frozen SL75 short intent and construct *offline* payload.

    The return value is a research-only dict; NO executable transport or
    serialization hook consumes it. The adapter must NEVER call it.
    """
    if not isinstance(intent, EntryIntent):
        raise ValueError("ENTRY_INTENT_INVALID")
    if type(leverage) is not int or not 1 <= leverage <= _MAX_RESEARCH_LEVERAGE:
        raise ValueError("CROSS_LEVERAGE_RESEARCH_BOUND")
    try:
        ref = _positive(intent.reference_price)
        notional = _positive(intent.notional_usdt)
        qty = _positive(intent.volume_contracts)
        stop = _positive(intent.stop_price)
        size = _positive(contract_size)
        step = _positive(volume_step)
        tick = _positive(price_tick)
    except ValueError:
        raise ValueError("CROSS_INTENT_NUMERIC_UNVERIFIED") from None
    if (
        type(intent.signal_id) is not int or intent.signal_id <= 0
        or type(intent.symbol) is not str
        or re.fullmatch(r"[A-Z0-9]+_USDT", intent.symbol) is None
        or intent.symbol in {"BTC_USDT", "ETH_USDT"}
        or type(intent.external_oid) is not str
        or not intent.external_oid.startswith("t100v1e")
        or not 8 <= len(intent.external_oid) <= 64
    ):
        raise ValueError("CROSS_INTENT_IDENTITY_UNVERIFIED")
    if qty % step != 0 or stop % tick != 0:
        raise ValueError("CROSS_CONTRACT_STEP_OR_TICK_MISMATCH")
    expected_stop = (ref * _STOP / tick).to_integral_value(
        rounding="ROUND_CEILING"
    ) * tick
    if stop != expected_stop:
        raise ValueError("CROSS_FROZEN_SL75_MISMATCH")
    actual_notional = qty * ref * size
    # Intent.builder floors quantity to volume step; never increase quantity.
    if actual_notional > notional or notional - actual_notional >= ref * size * step:
        raise ValueError("CROSS_INTENT_NOTIONAL_MISMATCH")
    return {
        "symbol": intent.symbol,
        "price": str(ref),
        "vol": str(qty),
        "leverage": leverage,
        "side": 3,  # open SHORT
        "type": 5,  # market
        "openType": 2,  # CROSS, distinct from existing isolated adapter
        "positionMode": 1,  # hedge
        "externalOid": intent.external_oid,
        "stopLossPrice": str(stop),
        "lossTrend": 1,
    }


def assess_cross_entry_offline(
    intent: EntryIntent, *, contract_size: Any,
    volume_step: Any, price_tick: Any, leverage: int,
    inventory: Mapping[str, Any] | None,
    margin_mode: Mapping[str, Any] | None,
    candidate_tier: Mapping[str, Any] | None,
) -> CrossEntryOfflineReview:
    """Review CURRENT evidence without allowing any live order or false pass.

    Even an apparently complete caller-supplied mapping cannot verify:
    - acceptance of a cross-mode order by THIS API key;
    - atomic stop materialization and fill-to-position coverage;
    - worst future tier/liquidation or wallet collateral;
    - adapter certification or explicit operator authorization.
    """
    try:
        payload = _hypothetical_payload(
            intent, contract_size=contract_size, volume_step=volume_step,
            price_tick=price_tick, leverage=leverage,
        )
    except (ValueError, TypeError, ArithmeticError):
        return CrossEntryOfflineReview(
            status="CROSS_RESEARCH_INTENT_INVALID_OR_UNVERIFIED",
            blockers=("HYPOTHETICAL_ORDER_SHAPE_UNVERIFIED",),
        )
    # Defensive check of the actual contract fields, not a name-only token.
    if (payload["openType"], payload["side"], payload["type"]) != (2, 3, 5):
        return CrossEntryOfflineReview(
            status="CROSS_RESEARCH_PAYLOAD_MODE_MISMATCH",
            blockers=("HYPOTHETICAL_ORDER_SHAPE_UNVERIFIED",),
        )

    inv = inventory if isinstance(inventory, Mapping) else {}
    mm = margin_mode if isinstance(margin_mode, Mapping) else {}
    tier = candidate_tier if isinstance(candidate_tier, Mapping) else {}

    inventory_consistent = (
        inv.get("account_wide_completeness_verified") is True
        and inv.get("critical_inventory_equal_across_witness_window") is True
    )
    positions = inv.get("after_positions")
    orders = inv.get("reported_open_order_counts")
    norders = orders.get("sumCount") if isinstance(orders, Mapping) else None
    flat = (
        inventory_consistent and type(positions) is int and positions == 0
        and type(norders) is int and norders == 0
        and inv.get("account_reconciled") is True
    )
    cross = (
        mm.get("status") == "MARGIN_MODE_CURRENT_CROSS_CORROBORATED_NONAUTHORIZING"
        and mm.get("independent_mode_reads_agree") is True
        and mm.get("private_cross_mode_corroborated") is True
    )
    current_tier = (
        tier.get("status") == "CANDIDATE_CURRENT_PRIVATE_TIER_COMPARED_NONAUTHORIZING"
        and tier.get("private_risk_two_get_consistent") is True
        and tier.get("candidate_quantity_evaluated") is True
        and tier.get("current_private_mmr_not_below_public") is True
        and tier.get("private_current_volume_cap_covers_candidate") is True
        and tier.get("higher_tier_requires_recheck") is False
    )
    blockers = []
    if not inventory_consistent:
        blockers.append("CURRENT_ACCOUNT_INVENTORY_NOT_VERIFIED")
    elif not flat:
        if inv.get("observed_nonflat_without_current_tpsl") is True:
            blockers.append("EXISTING_POSITION_WITHOUT_OBSERVED_NATIVE_STOP")
        if positions != 0:
            blockers.append("FIRST_PILOT_REQUIRES_FLAT_ACCOUNT")
        if norders != 0:
            blockers.append("FIRST_PILOT_REQUIRES_NO_OPEN_ORDERS")
        if inv.get("account_reconciled") is not True:
            blockers.append("ACCOUNT_NOT_RECONCILED")
    if not cross:
        blockers.append("CROSS_MARGIN_PRIVATE_MODE_NOT_CORROBORATED")
    if not current_tier:
        blockers.append("CANDIDATE_CURRENT_PRIVATE_TIER_NOT_VERIFIED")
    # Structural hard blockers that current GETs cannot establish.
    blockers.extend((
        "CROSS_MARGIN_ADAPTER_NOT_IMPLEMENTED",
        "ATTACHED_STOP_ATOMICITY_AND_FILL_COVERAGE_UNVERIFIED",
        "STRESSED_TIERS_AND_CROSS_WALLET_COLLATERAL_UNVERIFIED",
        "VENUE_LIQUIDATION_BEFORE_STOP_UNVERIFIED",
    ))
    return CrossEntryOfflineReview(
        status="CROSS_ENTRY_RESEARCH_REVIEW_BLOCKED",
        hypothetical_payload_shape_valid=True,
        current_inventory_consistent=inventory_consistent,
        current_account_flat_and_order_free=flat,
        private_current_cross_observed=cross,
        candidate_tier_currently_compared=current_tier,
        blockers=tuple(blockers),
    )

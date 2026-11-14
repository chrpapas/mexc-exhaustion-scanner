"""UNRELEASED V40 current 1–4-slot exchange risk identity composition.

This reconciles TWO independent, signed MEXC READ-ONLY observations:
* immutable checkpoint/entry order/native MARKET SL bookends (4-slot
  provenance and full account completeness); and
* current FAIR prices, country-specific public contract specifications,
  private CROSS-short risk tiers, positions and account asset economics.

The cross-risk reader's independently obtained second frame must be bound
to the previously verified two-frame risk observation by its stable
fingerprint. No caller-supplied portfolio quantity, leverage or contract
size is trusted. All existing V40-owned positions must match exactly.

Published current MMR is NOT a verified upper bound on future risk tiers.
MEXC's documented cashBalance is not certified cross-wallet collateral.
This module therefore computes a diagnostic 20%/75%/80% simultaneous fair
price shock but NEVER certifies liquidation survival, strategy parity,
native-stop execution, permission to send orders or to change stops.

No network transport, database writes, POST, credentials or Render changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D, InvalidOperation
from typing import Any

from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult, LedgerSnapshot, _ledger_digest, _venue_digest,
)
from app.t100_live_durable_recovery import (
    DurableRecoveryHalted, checkpoint_from_row,
)
from app.t100_v40_four_slot_guardian_policy import (
    audit_frozen_four_slot_native_stops,
)
from app.t100_live_cross_risk_observation import (
    CrossRiskRead, CrossRiskObservation, _static_hash,
)
from app.t100_live_risk_tier_witness import assess_risk_tier_witness


class V40PortfolioCurrentRiskHalted(ValueError):
    """Only fixed-status, non-secret failures."""


_SHOCKS=(D("0.20"), D("0.75"), D("0.80"))


@dataclass(frozen=True)
class V40PortfolioFairShock:
    adverse_price_pct: D
    modeled_unrealized_short_pnl_usdt: D
    modeled_gross_fair_notional_usdt: D
    current_published_mmr_reserve_usdt: D
    current_published_liquidation_fee_reserve_usdt: D
    # Does not have a certified futures cross wallet or future tier limit.
    exchange_liquidation_survival_certified: bool = False


@dataclass(frozen=True)
class V40PortfolioCurrentRisk:
    status: str
    slots: int = 0
    observed_entry_notional_usdt: D = D(0)
    current_gross_fair_notional_usdt: D = D(0)
    observed_account_equity_usdt: D = D(0)
    shocked: tuple[V40PortfolioFairShock, ...] = ()
    all_owned_positions_and_native_stops_verified: bool = False
    same_venue_inventory_in_both_readers: bool = False
    current_private_public_tiers_verified: bool = False
    future_tier_ceiling_verified: bool = False
    cross_wallet_collateral_verified: bool = False
    liquidation_before_sl75_excluded: bool = False
    native_sl75_execution_certified: bool = False
    historical_strategy_parity_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_posts: int = 0
    database_writes: int = 0


def _positive(raw: Any) -> D:
    if raw is None or type(raw) is bool or type(raw) not in (D,int,float,str):
        raise V40PortfolioCurrentRiskHalted("V40_RISK_INVALID_POSITIVE_NUMBER")
    try:
        v=D(str(raw))
    except (ValueError,InvalidOperation,TypeError):
        raise V40PortfolioCurrentRiskHalted("V40_RISK_INVALID_POSITIVE_NUMBER") from None
    if not v.is_finite() or v<=0:
        raise V40PortfolioCurrentRiskHalted("V40_RISK_INVALID_POSITIVE_NUMBER")
    return v


def audit_current_four_slot_risk(
    *, snapshot: FourSlotSnapshotResult, ledger: LedgerSnapshot,
    risk_observation: CrossRiskObservation, risk_read: CrossRiskRead,
    now_ms: int, max_age_ms: int = 2500,
) -> V40PortfolioCurrentRisk:
    """Bound distinct account/native-stop and cross-risk witnesses to one inventory.

    A matching hash is evidence of matching REST critical fields only; it
    cannot establish cryptographic authentication, distributed atomicity,
    cross-margin wallet semantics, future margin tiers or venue stop fills.
    The actual reader injection boundary is independently responsible for
    verifying the two transports use the SAME isolated subaccount.
    """
    block=lambda status:V40PortfolioCurrentRisk(status)
    if (
        type(snapshot) is not FourSlotSnapshotResult
        or type(ledger) is not LedgerSnapshot
        or type(risk_observation) is not CrossRiskObservation
        or type(risk_read) is not CrossRiskRead
        or type(now_ms) is not int
        or type(max_age_ms) is not int
        or not 1<=max_age_ms<=5000
        or type(risk_read.observed_at_ms) is not int
        or not 0<=now_ms-risk_read.observed_at_ms<=max_age_ms
    ):
        return block("V40_CURRENT_RISK_INPUT_OR_FRESHNESS_INVALID")
    if (
        snapshot.status!="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"
        or snapshot.occupied_slots not in (1,2,3,4)
        or snapshot.recovery is None
        or snapshot.inventory is None
        or snapshot.entry_allowed is not False
        or snapshot.live_authorization is not False
        or ledger.readonly_repeatable_read is not True
        or risk_observation.status!="CURRENT_CROSS_RISK_OBSERVED_NONAUTHORIZING"
        or risk_observation.observed_positions!=snapshot.occupied_slots
        or risk_observation.stable_critical_inventory is not True
        or risk_observation.position_margin_economic_witness_observed is not True
        or risk_observation.current_risk_tiers_observed is not True
        or risk_observation.fair_prices_observed is not True
        or risk_observation.native_stop_protection_verified is not False
        or risk_observation.future_stressed_tiers_certified is not False
        or risk_observation.venue_liquidation_certified is not False
        or risk_observation.account_wide_completeness_certified is not False
        or risk_observation.entry_allowed is not False
        or risk_observation.live_authorization is not False
        or any(type(getattr(risk_read,k)) is not bool
               or getattr(risk_read,k) is not True
               for k in ("positions_complete","stops_complete","assets_complete"))
    ):
        return block("V40_CURRENT_RISK_SIGNED_ACCOUNT_OR_RISK_WINDOW_NOT_VERIFIED")
    try:
        if (
            snapshot.ledger_hash!=_ledger_digest(ledger)
            or snapshot.critical_inventory_hash!=_venue_digest(
                risk_read.positions,risk_read.stops,
            )
            or risk_observation.evidence_fingerprint_sha256!=_static_hash(risk_read)
            or risk_observation.observed_current_stops!=len(risk_read.stops)
        ):
            return block("V40_CURRENT_RISK_INDEPENDENT_READ_INVENTORY_DRIFT")
    except (ValueError,TypeError,KeyError,InvalidOperation):
        return block("V40_CURRENT_RISK_INDEPENDENT_READ_INVENTORY_DRIFT")
    try:
        native=audit_frozen_four_slot_native_stops(
            snapshot=snapshot,ledger=ledger,
            account_positions=risk_read.positions,
            account_stops=risk_read.stops,
        )
    except Exception:
        return block("V40_CURRENT_RISK_OWNED_NATIVE_SL_PROVENANCE_INVALID")
    if not (
        native.status==
            "V40_FOUR_SLOT_ACCOUNT_NATIVE_MARKET_SL_SIGNED_GET_NONAUTHORIZING"
        and native.protected_owned_positions==snapshot.occupied_slots
        and native.matching_native_market_stops==snapshot.occupied_slots
        and native.all_owned_cross_positions_verified is True
        and native.separate_symbols_verified is True
        and native.account_bookends_verified is True
        and native.ledger_bookends_verified is True
        and native.entry_allowed is False
        and native.live_authorization is False
        and native.mexc_posts==native.database_writes==0
    ):
        return block("V40_CURRENT_RISK_OWNED_NATIVE_SL_PROVENANCE_INVALID")
    try:
        checkpoints=tuple(checkpoint_from_row(cp) for cp in ledger.checkpoints)
        by_sym={cp.symbol:cp for cp in checkpoints}
        positions={x["symbol"]:x for x in risk_read.positions}
        symbols=set(by_sym)
        if not (
            len(symbols)==snapshot.occupied_slots==len(positions)==len(risk_read.stops)
            and symbols==set(positions)
            and symbols==set(risk_read.private_risk_by_symbol)
            and symbols==set(risk_read.public_contract_by_symbol)
            and symbols==set(risk_read.fair_by_symbol)
        ):
            return block("V40_CURRENT_RISK_SYMBOL_OR_POSITION_SET_DRIFT")
        usdt=[a for a in risk_read.assets
              if type(a) is dict and a.get("currency")=="USDT"]
        if len(usdt)!=1:
            return block("V40_CURRENT_RISK_USDT_ASSET_NOT_UNIQUE")
        equity=_positive(usdt[0].get("equity"))
        # Equity is observed, NOT promoted to a certified cross margin wallet.
        entry_total=D(0)
        current_total=D(0)
        rows=[]
        for sym in sorted(symbols):
            cp=by_sym[sym]
            pos=positions[sym]
            quantity=_positive(pos.get("holdVol"))
            if (
                cp.lifecycle_state!="OPEN"
                or str(pos.get("positionId") or "")!=cp.position_id
                or type(pos.get("leverage")) is not int
                or pos.get("positionType")!=2 or pos.get("openType")!=2
                or quantity!=cp.contracts
            ):
                return block("V40_CURRENT_RISK_EXACT_OWNED_LOT_DRIFT")
            private=risk_read.private_risk_by_symbol[sym]
            public=risk_read.public_contract_by_symbol[sym]
            tier=assess_risk_tier_witness(
                symbol=sym,contracts=quantity,
                private_risk={sym:private},public_contract=public,
            )
            if not (
                tier.status==
                    "CURRENT_CUSTOM_VOLUME_RISK_SCHEDULE_OBSERVED_NONAUTHORIZING"
                and tier.configured_leverage==pos["leverage"]
                and tier.current_risk_row_observed is True
                and tier.public_static_tier_schedule_observed is True
                and tier.stressed_tier_upper_bound_verified is False
                and tier.live_authorization is False
            ):
                return block("V40_CURRENT_RISK_PRIVATE_LEVERAGE_OR_TIER_DRIFT")
            fair_data=risk_read.fair_by_symbol[sym]
            if (
                type(fair_data) is not dict
                or type(fair_data.get("observed_at_ms")) is not int
                or not 0<=now_ms-fair_data["observed_at_ms"]<=max_age_ms
            ):
                return block("V40_CURRENT_RISK_UNDATED_FAIR_PRICE")
            fair=_positive(fair_data.get("fairPrice"))
            avg=_positive(pos.get("holdAvgPrice"))
            if abs(avg-cp.entry_price)>max(D("0.00000001"),cp.entry_price*D("0.00000001")):
                return block("V40_CURRENT_RISK_ENTRY_PRICE_DRIFT")
            if not (
                tier.contract_size>0
                and D(0)<tier.conservative_schedule_mmr<D(1)
                and D(0)<=tier.liquidation_fee_rate<D(1)
            ):
                return block("V40_CURRENT_RISK_PUBLISHED_TIER_UNUSABLE")
            base=quantity*tier.contract_size
            entry_total+=base*cp.entry_price
            current_total+=base*fair
            rows.append((base,cp.entry_price,fair,
                         tier.conservative_schedule_mmr,
                         tier.liquidation_fee_rate))
        shocks=[]
        for adverse in _SHOCKS:
            pnl=D(0)
            notional=D(0)
            mmr=D(0)
            liquidation_fee=D(0)
            for base,entry,fair,m,fee in rows:
                stress=fair*(D(1)+adverse)
                scenario_notional=base*stress
                pnl+=(entry-stress)*base
                notional+=scenario_notional
                mmr+=max(base*entry,scenario_notional)*m
                liquidation_fee+=scenario_notional*fee
            shocks.append(V40PortfolioFairShock(
                adverse_price_pct=adverse,
                modeled_unrealized_short_pnl_usdt=pnl,
                modeled_gross_fair_notional_usdt=notional,
                current_published_mmr_reserve_usdt=mmr,
                current_published_liquidation_fee_reserve_usdt=liquidation_fee,
            ))
        return V40PortfolioCurrentRisk(
            status="V40_CURRENT_EXACT_FOUR_SLOT_FAIR_AND_TIER_RESEARCH_NONAUTHORIZING",
            slots=snapshot.occupied_slots,
            observed_entry_notional_usdt=entry_total,
            current_gross_fair_notional_usdt=current_total,
            observed_account_equity_usdt=equity,
            shocked=tuple(shocks),
            all_owned_positions_and_native_stops_verified=True,
            same_venue_inventory_in_both_readers=True,
            current_private_public_tiers_verified=True,
        )
    except (DurableRecoveryHalted,ValueError,KeyError,TypeError,ArithmeticError):
        return block("V40_CURRENT_RISK_ECONOMIC_SCHEMA_OR_NUMBER_INVALID")

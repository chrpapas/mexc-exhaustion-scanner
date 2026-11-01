"""Redacted, GET-only mixed CROSS/ISOLATED MEXC risk tier schema triage.

Do not mistake the private/current risk level for the worst risk tier at a
future price/size. Public riskLimitMode=INCREASE with BY_VOLUME does NOT
authorize reconstructing a liquidation schedule: verify the venue's exact
incremental tier conventions and current private mode independently first.

No candidate, balance, symbol, trade ID, rate, tier or margin values emitted.
Does not read credentials or issue any venue calls. No real-money permit.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

@dataclass(frozen=True)
class MixedTierShape:
    status: str
    private_current_row_gap: str = "NOT_CHECKED"
    positions_checked: int = 0
    current_position_side_private_rows_present: bool = False
    incremental_volume_contracts_observed: int = 0
    custom_volume_contracts_observed: int = 0
    incremental_fields_well_formed: bool = False
    risk_tiers_current_certified: bool = False
    risk_tiers_at_stress_certified: bool = False
    cross_wallet_collateral_certified: bool = False
    liquidation_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    read_only: bool = True
    mexc_orders_sent: bool = False


def _positive(x: Any) -> Decimal | None:
    if x is None or type(x) is bool:
        return None
    try:
        v = Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return v if v.is_finite() and v > 0 else None


def _nonnegative(x: Any) -> Decimal | None:
    if x is None or type(x) is bool:
        return None
    try:
        v = Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return v if v.is_finite() and v >= 0 else None


def assess_mixed_tier_shape(
    positions: Any,
    private_risk_by_symbol: Any,
    public_contract_by_symbol: Any,
) -> MixedTierShape:
    def blocked(code: str, **kw: Any) -> MixedTierShape:
        return MixedTierShape(status=code, **kw)

    if (
        type(positions) is not list or not 1 <= len(positions) <= 4
        or any(type(p) is not dict for p in positions)
        or type(private_risk_by_symbol) is not dict
        or type(public_contract_by_symbol) is not dict
    ):
        return blocked("MIXED_TIER_INPUT_OR_ACCOUNT_SCOPE_UNVERIFIED")
    symbols = [p.get("symbol") for p in positions]
    if (
        any(type(s) is not str or not s.endswith("_USDT") for s in symbols)
        or len(set(symbols)) != len(symbols)
        or set(symbols) != set(private_risk_by_symbol)
        or set(symbols) != set(public_contract_by_symbol)
    ):
        return blocked("MIXED_TIER_SYMBOL_SCOPES_UNVERIFIED")

    incremental = 0
    custom = 0
    all_incremental_fields = True
    for p in positions:
        symbol = p["symbol"]
        contract = public_contract_by_symbol[symbol]
        private_rows = private_risk_by_symbol[symbol]
        if (
            type(contract) is not dict
            or contract.get("symbol") != symbol
            or type(private_rows) is not list
            or not private_rows
            or any(type(row) is not dict for row in private_rows)
        ):
            return blocked("MIXED_TIER_CONTRACT_OR_PRIVATE_SHAPE_UNVERIFIED")
        mode = contract.get("riskLimitMode")
        limit_type = contract.get("riskLimitType")
        if (mode, limit_type) not in {
            ("INCREASE", "BY_VOLUME"), ("CUSTOM", "BY_VOLUME")
        }:
            return blocked("MIXED_TIER_UNSUPPORTED_RISK_SCHEDULE")
        if (
            _positive(contract.get("contractSize")) is None
            or _nonnegative(contract.get("liquidationFeeRate")) is None
            or contract.get("state") != 0
            or contract.get("futureType") != 1
            or contract.get("quoteCoin") != "USDT"
            or contract.get("settleCoin") != "USDT"
        ):
            return blocked("MIXED_TIER_VENUE_CONTRACT_TERMS_UNVERIFIED")
        side = p.get("positionType")
        margin_mode = p.get("openType")
        if (
            type(side) is not int or side not in {1, 2}
            or type(margin_mode) is not int or margin_mode not in {1, 2}
            or _positive(p.get("holdVol")) is None
        ):
            return blocked("MIXED_TIER_POSITION_SIDE_OR_MODE_UNVERIFIED")
        # MEXC's current risk_limit response documents positionType and
        # openType. Classify the missing proof without exposing either side,
        # margin type, symbol, or venue values in the operator output.
        side_rows = [
            row for row in private_rows
            if row.get("symbol") == symbol
            and row.get("positionType") == side
        ]
        if not side_rows:
            return blocked(
                "MIXED_TIER_CURRENT_PRIVATE_SIDE_OR_MODE_UNVERIFIED",
                private_current_row_gap="PRIVATE_POSITION_SIDE_ROW_ABSENT",
            )
        if len(side_rows) != 1:
            return blocked(
                "MIXED_TIER_CURRENT_PRIVATE_SIDE_OR_MODE_UNVERIFIED",
                private_current_row_gap="PRIVATE_POSITION_SIDE_ROWS_AMBIGUOUS",
            )
        row = side_rows[0]
        if type(row.get("openType")) is not int or row["openType"] not in (1, 2):
            return blocked(
                "MIXED_TIER_CURRENT_PRIVATE_SIDE_OR_MODE_UNVERIFIED",
                private_current_row_gap="PRIVATE_MARGIN_MODE_FIELD_UNVERIFIED",
            )
        if row["openType"] != margin_mode:
            return blocked(
                "MIXED_TIER_CURRENT_PRIVATE_SIDE_OR_MODE_UNVERIFIED",
                private_current_row_gap="PRIVATE_MARGIN_MODE_MISMATCH",
            )
        if (
            type(row.get("level")) is not int or row["level"] < 1
            or _positive(row.get("maxVol")) is None
            or _positive(row.get("mmr")) is None
            or _positive(row.get("imr")) is None
            or _positive(row.get("maxLeverage")) is None
            or _positive(row.get("leverage")) is None
        ):
            return blocked(
                "MIXED_TIER_PRIVATE_CURRENT_RATE_FIELDS_UNVERIFIED",
                private_current_row_gap="PRIVATE_CURRENT_RATE_FIELDS_UNVERIFIED",
            )
        # These are a valid-looking CURRENT risk row, not the worst case
        # across adverse price moves, partial liquidation or a new order.
        if mode == "INCREASE":
            incremental += 1
            fields = (
                "riskBaseVol", "riskIncrVol", "riskLevelLimit",
                "riskIncrMmr", "riskIncrImr",
                "maintenanceMarginRate", "initialMarginRate",
            )
            if (
                _positive(contract.get("riskBaseVol")) is None
                or _positive(contract.get("riskIncrVol")) is None
                or type(contract.get("riskLevelLimit")) is not int
                or contract["riskLevelLimit"] < 1
                or any(_nonnegative(contract.get(key)) is None
                       for key in fields[3:])
            ):
                all_incremental_fields = False
        else:
            custom += 1
            if (
                type(contract.get("riskLimitCustom")) is not list
                or not contract["riskLimitCustom"]
            ):
                return blocked("MIXED_TIER_CUSTOM_SCHEDULE_UNVERIFIED")

    return MixedTierShape(
        private_current_row_gap="CURRENT_SIDE_MODE_ROW_OBSERVED_NONAUTHORIZING",
        status=(
            "MIXED_INCREMENTAL_VOLUME_TERMS_OBSERVED_UNMODELED"
            if incremental and all_incremental_fields else
            "MIXED_INCREMENTAL_VOLUME_TERMS_INCOMPLETE"
            if incremental else
            "MIXED_CUSTOM_VOLUME_TERMS_OBSERVED_NONAUTHORIZING"
        ),
        positions_checked=len(positions),
        current_position_side_private_rows_present=True,
        incremental_volume_contracts_observed=incremental,
        custom_volume_contracts_observed=custom,
        incremental_fields_well_formed=bool(incremental and all_incremental_fields),
    )

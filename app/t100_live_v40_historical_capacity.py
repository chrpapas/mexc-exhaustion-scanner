"""Historical V40 admission vs signal-time PUBLIC contract capacity (offline).

For an already-exported V40 entry, reconstruct the pre-lot REFERENCE notional
from saved V40 diagnostics. Do NOT compare just the HYBRID/KNOWN_ONLY entry
'notional': that value may ALREADY have been clipped by historical lot rules.

Only accepts locally supplied, time-stamped public contract snapshots and
signal-time reference prices. Missing/stale/future witnesses are NOT replaced
with present-day contract metadata. Inputs are research provenance, NOT
independently authenticated MEXC history. Pure function; no network, POST,
private account data, DB, or authorization.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.t100_live_public_v40_capacity import assess_public_v40_capacity

D = Decimal
MAX_CONTRACT_SNAPSHOT_AGE_SECONDS = 86400
MAX_PRICE_SNAPSHOT_AGE_SECONDS = 1800
_TOL = D("0.000003")


@dataclass(frozen=True)
class HistoricalV40CapacityDecision:
    status: str
    symbol: str = ""
    signal_time: str = ""
    pre_lot_reference_notional_usdt: D | None = None
    exported_entry_notional_usdt: D | None = None
    signal_reference_price_usdt: D | None = None
    lot_count_for_reference: D | None = None
    published_max_position_lots: D | None = None
    published_max_single_order_lots: D | None = None
    exported_entry_below_reference: bool = False
    public_position_tier_fits: bool = False
    public_single_order_fits: bool = False
    point_in_time_evidence_supplied: bool = False
    point_in_time_venue_authenticity_certified: bool = False
    historical_execution_capacity_certified: bool = False
    private_tier_verified: bool = False
    liquidity_depth_verified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _number(value: Any, *, allow_zero: bool = False) -> D:
    if value is None or type(value) is bool:
        raise ValueError("INVALID_NUMBER")
    try:
        num = D(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("INVALID_NUMBER") from None
    if not num.is_finite() or (num < 0 if allow_zero else num <= 0):
        raise ValueError("INVALID_NUMBER")
    return num


def _utc(raw: Any) -> datetime:
    if type(raw) is not str or not raw:
        raise ValueError("INVALID_TIMESTAMP")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("INVALID_TIMESTAMP") from None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("TIMESTAMP_MUST_BE_TIMEZONE_AWARE")
    return value.astimezone(timezone.utc)


def _asof_valid(observed: Any, signal: datetime, max_age_seconds: int) -> bool:
    try:
        asof = _utc(observed)
    except ValueError:
        return False
    age = (signal - asof).total_seconds()
    return 0 <= age <= max_age_seconds


def assess_historical_v40_entry_capacity(
    exported_entry: Any, evidence: Any,
    *, leverage: int = 1,
) -> HistoricalV40CapacityDecision:
    def block(status: str, **fields: Any) -> HistoricalV40CapacityDecision:
        return HistoricalV40CapacityDecision(status=status, **fields)

    if type(exported_entry) is not dict:
        return block("V40_HISTORICAL_ENTRY_INVALID")
    symbol = exported_entry.get("symbol")
    signal_raw = exported_entry.get("time")
    try:
        if type(symbol) is not str or not symbol:
            raise ValueError("INVALID_SYMBOL")
        signal = _utc(signal_raw)
        requested = _number(exported_entry.get("desired_before_liquidity"))
        quota = _number(exported_entry.get("prior_turnover_quota_usdt"),
                        allow_zero=True)
        p25 = _number(exported_entry.get("p25_turnover"))
        exit_capacity = _number(exported_entry.get("exit_capacity_limit"))
        actual = _number(exported_entry.get("notional"))
        reference = min(requested, quota, p25 * D("0.05"), exit_capacity)
        if reference <= 0 or actual > reference + max(_TOL, reference * D("0.0000000002")):
            return block("V40_HISTORICAL_ENTRY_REFERENCE_RECONSTRUCTION_DRIFT")
    except (TypeError, ValueError, ArithmeticError, InvalidOperation):
        return block("V40_HISTORICAL_ENTRY_INVALID")

    common = dict(
        symbol=symbol, signal_time=signal.isoformat(),
        pre_lot_reference_notional_usdt=reference,
        exported_entry_notional_usdt=actual,
        exported_entry_below_reference=actual + _TOL < reference,
    )
    if type(evidence) is not dict:
        return block("V40_HISTORICAL_POINT_IN_TIME_EVIDENCE_MISSING", **common)
    if (
        evidence.get("symbol") != symbol
        or type(evidence.get("time")) is not str
    ):
        return block("V40_HISTORICAL_EVIDENCE_IDENTITY_INVALID", **common)
    try:
        same_time = _utc(evidence["time"]) == signal
    except ValueError:
        same_time = False
    if not same_time:
        return block("V40_HISTORICAL_EVIDENCE_IDENTITY_INVALID", **common)

    if not _asof_valid(evidence.get("contract_observed_at"), signal,
                       MAX_CONTRACT_SNAPSHOT_AGE_SECONDS):
        return block("V40_HISTORICAL_CONTRACT_SNAPSHOT_NOT_POINT_IN_TIME", **common)
    if not _asof_valid(evidence.get("price_observed_at"), signal,
                       MAX_PRICE_SNAPSHOT_AGE_SECONDS):
        return block("V40_HISTORICAL_PRICE_NOT_POINT_IN_TIME", **common)
    try:
        price = _number(evidence.get("entry_reference_price"))
    except ValueError:
        return block("V40_HISTORICAL_PRICE_INVALID", **common)
    contract = evidence.get("contract")
    if type(contract) is not dict or contract.get("symbol") != symbol:
        return block("V40_HISTORICAL_CONTRACT_IDENTITY_INVALID", **common)
    result = assess_public_v40_capacity(
        contract, desired_notional_usdt=reference,
        entry_reference_price=price, leverage=leverage,
    )
    classified = dict(
        **common, signal_reference_price_usdt=price,
        lot_count_for_reference=result.required_lots_rounded_down,
        published_max_position_lots=result.public_max_position_tier_lots,
        published_max_single_order_lots=result.public_max_single_order_lots,
        public_position_tier_fits=result.fits_public_position_tier,
        public_single_order_fits=result.fits_public_single_order_limit,
        point_in_time_evidence_supplied=True,
    )
    statuses = {
        "V40_CAPACITY_PUBLISHED_POSITION_TIER_EXCEEDED_NONAUTHORIZING":
            "V40_HISTORICAL_PUBLIC_POSITION_CAP_REJECT",
        "V40_CAPACITY_PUBLISHED_SINGLE_ORDER_LIMIT_EXCEEDED_NONAUTHORIZING":
            "V40_HISTORICAL_PUBLIC_SINGLE_ORDER_CAP_REJECT",
        "V40_CAPACITY_PUBLISHED_SINGLE_ORDER_FITS_NONAUTHORIZING":
            "V40_HISTORICAL_PUBLIC_CAP_FITS_NONAUTHORIZING",
        "V40_CAPACITY_TARGET_BELOW_MINIMUM_NONAUTHORIZING":
            "V40_HISTORICAL_PUBLIC_MINIMUM_ORDER_NOT_MET",
    }
    return block(statuses.get(result.status,
                              "V40_HISTORICAL_PUBLIC_METADATA_UNVERIFIED"),
                 **classified)

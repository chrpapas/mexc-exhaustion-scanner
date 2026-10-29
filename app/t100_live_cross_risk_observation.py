"""Offline-testable GET-only cross-risk observation *witness* for T100/V40.

Each read is supplied by an injected read-only reader, not by this module.
The reader must source complete account positions, stops, assets, private
risk-limit rows, public contract metadata and timestamped FAIR prices.
Two stable observations reduce torn-read risk; MEXC cannot guarantee an
atomic distributed snapshot and observations do not verify position ownership.

Critically: MEXC documents `cashBalance` as WITHDRAWABLE balance, not
a verified cross-margin wallet collateral figure. This module never
converts cashBalance or equity into CrossAccount.wallet_balance_usdt.
Risk-tier witness remains CURRENT ONLY, NOT future-tier certification.

This module cannot issue a permit, set leverage, create orders or POST.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Protocol

from app.t100_live_consistency import CRITICAL_POSITION_FIELDS, CRITICAL_STOP_FIELDS
from app.t100_live_position_margin_witness import assess_position_margin_witness
from app.t100_live_risk_tier_witness import assess_risk_tier_witness

MAX_POSITIONS = 4

# Fixed codes only. Never copy raw exchange errors or exception messages to
# operator-facing output. Unknown errors receive a generic fixed code.
SAFE_FRAME_FAILURE_CODES = frozenset({
    "BAD_FRAME_TYPE", "STALE_OR_INCOMPLETE_FRAME",
    "MALFORMED_RISK_COLLECTION", "MARGIN_ECONOMIC_WITNESS_MISMATCH",
    "USDT_ASSET_NOT_UNIQUE", "ASSET_COLLATERAL_SCHEMA_INCOMPLETE",
    "BOOL_NOT_DECIMAL", "BAD_DECIMAL",
    "POSITION_NOT_VERIFIED_CROSS_SHORT", "DUPLICATE_POSITION_IDENTITY",
    "MALFORMED_STOP_ROW", "PROTECTION_IDENTITY_INVALID",
    "DUPLICATE_STOP_IDENTITY", "RISK_MARK_SYMBOL_SET_MISMATCH",
    "FAIR_MARK_UNDATED", "RISK_TIER_CURRENT_ONLY_UNVERIFIED",
    "RISK_TIER_SYMBOL_INVALID", "RISK_TIER_SYMBOL_RESPONSE_MISMATCH",
    "RISK_TIER_PRIVATE_ROWS_MISSING", "RISK_TIER_PRIVATE_SHORT_NOT_UNIQUE",
    "RISK_TIER_PRIVATE_SYMBOL_DRIFT", "RISK_TIER_PRIVATE_CROSS_OR_CAP_UNVERIFIED",
    "RISK_TIER_PRIVATE_MMR_INVALID", "RISK_TIER_CURRENT_MMR_INVALID",
    "RISK_TIER_STATIC_VOLUME_SCHEDULE_UNAVAILABLE",
    "RISK_TIER_PUBLIC_CONTRACT_NOT_ELIGIBLE",
    "RISK_TIER_LIQUIDATION_FEE_INVALID",
    "RISK_TIER_PUBLIC_LEVERAGE_LIMIT", "RISK_TIER_CUSTOM_LEVELS_MISSING",
    "RISK_TIER_LEVEL_SEQUENCE_INVALID", "RISK_TIER_LEVEL_MONOTONICITY_INVALID",
    "RISK_TIER_PRIVATE_LEVEL_NOT_IN_SCHEDULE",
    "RISK_TIER_PRIVATE_PUBLIC_MMR_CONFLICT",
    "RISK_TIER_POSITION_EXCEEDS_PUBLISHED_TIERS",
    "RISK_TIER_POSITION_LEVERAGE_EXCEEDS_TIER", "RISK_TIER_INPUT_INVALID",
})
SAFE_COLLECTOR_FAILURE_CODES = frozenset({
    "COLLECTOR_CLOCK_INVALID", "COLLECTOR_NONFLAT_ONE_TO_FOUR_REQUIRED",
    "COLLECTOR_POSITION_SYMBOLS_INVALID", "COLLECTOR_PRIVATE_RISK_SCOPE_DRIFT",
    "PUBLIC_RISK_GET_FAILED", "PUBLIC_RISK_RESPONSE_UNVERIFIED",
    "COLLECTOR_FAIR_TIMESTAMP_OR_PRICE_INVALID",
    "COLLECTOR_READ_WINDOW_STALE", "COLLECTOR_INVENTORY_INCOMPLETE",
    "COLLECTOR_PRE_MARGIN_WITNESS_UNVERIFIED",
    "COLLECTOR_POST_MARGIN_WITNESS_UNVERIFIED",
    "COLLECTOR_MARGIN_WITNESS_UNCLASSIFIED",
    *( "COLLECTOR_" + phase + "_" + kind
       for phase in ("PRE", "POST")
       for kind in (
           "MARGIN_SCHEMA_UNVERIFIED",
           "POSITION_MARGIN_MISMATCH",
           "UNREALIZED_PNL_MISMATCH",
           "OTHER_ASSET_EXPOSURE_UNVERIFIED",
       )),
    "COLLECTOR_READ_FAILED",
    *( "COLLECTOR_READ_FAILED_AT_" + phase for phase in (
        "CLOCK", "PRIVATE_POSITIONS", "PRIVATE_STOPS",
        "SYMBOL_CROSSCHECK", "ASSETS", "MARGIN_WITNESS", "ORDER_COUNTS",
        "PRIVATE_RISK", "PUBLIC_CONTRACT", "PUBLIC_FAIR",
        "POST_POSITIONS", "POST_STOPS", "POST_ASSETS",
        "POST_MARGIN_WITNESS", "FINAL_CLOCK",
        "CONSISTENCY", "CERTIFICATE",
    )),
})
SAFE_FAILURE_STAGES = frozenset({
    "NONE", "READ_FIRST", "VALIDATE_FIRST", "READ_SECOND", "VALIDATE_SECOND",
})


@dataclass(frozen=True)
class CrossRiskRead:
    observed_at_ms: int
    positions: list[dict[str, Any]]
    stops: list[dict[str, Any]]
    assets: list[dict[str, Any]]
    private_risk_by_symbol: dict[str, list[dict[str, Any]]]
    public_contract_by_symbol: dict[str, dict[str, Any]]
    fair_by_symbol: dict[str, dict[str, Any]]
    positions_complete: bool
    stops_complete: bool
    assets_complete: bool


class CrossRiskReader(Protocol):
    async def read(self) -> CrossRiskRead: ...


@dataclass(frozen=True)
class CrossRiskObservation:
    status: str
    observed_positions: int = 0
    observed_current_stops: int = 0
    native_stop_protection_verified: bool = False
    stable_critical_inventory: bool = False
    position_margin_economic_witness_observed: bool = False
    current_risk_tiers_observed: bool = False
    fair_prices_observed: bool = False
    evidence_fingerprint_sha256: str | None = None
    account_wide_completeness_certified: bool = False
    future_stressed_tiers_certified: bool = False
    venue_liquidation_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    failure_stage: str = "NONE"
    failure_code: str = "NONE"


def _positive(raw: Any) -> Decimal:
    if type(raw) is bool:
        raise ValueError("BOOL_NOT_DECIMAL")
    try:
        value = Decimal(str(raw))
    except (ValueError, InvalidOperation, TypeError) as exc:
        raise ValueError("BAD_DECIMAL") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("BAD_DECIMAL")
    return value


def _nonnegative(raw: Any) -> Decimal:
    if type(raw) is bool:
        raise ValueError("BOOL_NOT_DECIMAL")
    try:
        value = Decimal(str(raw))
    except (ValueError, InvalidOperation, TypeError):
        raise ValueError("BAD_DECIMAL") from None
    if not value.is_finite() or value < 0:
        raise ValueError("BAD_DECIMAL")
    return value


def _canonical(raw: Any) -> str:
    # Do not stringify arbitrary object reprs or tolerate NaN JSON.
    def encode(value: Any) -> str:
        if isinstance(value, Decimal) and value.is_finite():
            return str(value)
        raise TypeError("UNSUPPORTED_VALUE")
    return json.dumps(raw, sort_keys=True, default=encode,
                      allow_nan=False, separators=(",", ":"))


def _static_hash(read: CrossRiskRead) -> str:
    # MTM and fair price are deliberately excluded: they change rapidly.
    # Economic consistency and bounded fair drift are verified separately.
    def project(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[str]:
        return sorted(_canonical({k: v for k, v in row.items() if k in fields})
                      for row in rows)
    src = {
        "positions": project(read.positions, CRITICAL_POSITION_FIELDS),
        "stops": project(read.stops, CRITICAL_STOP_FIELDS),
        "private_risk": _canonical(read.private_risk_by_symbol),
        "contract": _canonical(read.public_contract_by_symbol),
        # Exclude rapidly changing unrealized PNL/equity from the bookend,
        # but block cash transfers, bonus/debt and frozen order-margin drift.
        "asset_cash": sorted(_canonical({
            key: row[key] for key in (
                "currency", "cashBalance", "frozenBalance", "bonus",
                "debtAmount", "contributeMarginAmount",
            ) if key in row
        }) for row in read.assets),
    }
    return hashlib.sha256(_canonical(src).encode()).hexdigest()


def _frame(
    read: CrossRiskRead, *,
    now_ms: int, max_age_ms: int,
) -> tuple[tuple[str, ...], dict[str, Decimal], str]:
    if type(read) is not CrossRiskRead:
        raise ValueError("BAD_FRAME_TYPE")
    if (
        type(read.observed_at_ms) is not int
        or type(now_ms) is not int
        or read.observed_at_ms > now_ms + 100
        or now_ms - read.observed_at_ms > max_age_ms
        or any(type(getattr(read, x)) is not bool or getattr(read, x) is not True
               for x in ("positions_complete", "stops_complete", "assets_complete"))
    ):
        raise ValueError("STALE_OR_INCOMPLETE_FRAME")
    if (
        not isinstance(read.positions, list)
        or not 1 <= len(read.positions) <= MAX_POSITIONS
        or not isinstance(read.stops, list)
        or len(read.stops) > 100
        or not isinstance(read.assets, list)
        or not isinstance(read.private_risk_by_symbol, dict)
        or not isinstance(read.public_contract_by_symbol, dict)
        or not isinstance(read.fair_by_symbol, dict)
        or any(type(x) is not dict for x in read.positions + read.stops + read.assets)
    ):
        raise ValueError("MALFORMED_RISK_COLLECTION")

    economic = assess_position_margin_witness(read.positions, read.assets)
    if economic.get("position_inventory_economic_witness_observed") is not True:
        raise ValueError("MARGIN_ECONOMIC_WITNESS_MISMATCH")
    usdt_assets = [x for x in read.assets if x.get("currency") == "USDT"]
    if len(usdt_assets) != 1:
        raise ValueError("USDT_ASSET_NOT_UNIQUE")
    for key in ("cashBalance", "equity", "availableBalance"):
        if key not in usdt_assets[0]:
            raise ValueError("ASSET_COLLATERAL_SCHEMA_INCOMPLETE")
        # Zero withdrawable and available balances are valid observations
        # on a margin-constrained account. This is NOT a trade-readiness
        # test or a verified cross-wallet collateral determination.
        if key == "equity":
            _positive(usdt_assets[0][key])
        else:
            _nonnegative(usdt_assets[0][key])

    symbols = []
    pos_ids = []
    marks: dict[str, Decimal] = {}
    for pos in read.positions:
        symbol = pos.get("symbol")
        pid = str(pos.get("positionId") or "")
        if (
            not isinstance(symbol, str)
            or not pid.isdigit() or int(pid) <= 0
            or pos.get("positionType") != 2
            or type(pos.get("positionType")) is not int
            or pos.get("openType") != 2
            or type(pos.get("openType")) is not int
        ):
            raise ValueError("POSITION_NOT_VERIFIED_CROSS_SHORT")
        symbols.append(symbol)
        pos_ids.append(pid)
    if len(set(symbols)) != len(symbols) or len(set(pos_ids)) != len(pos_ids):
        raise ValueError("DUPLICATE_POSITION_IDENTITY")
    # Account observation does NOT claim that any current TP/SL row is
    # protective, linked to an owned T100 position, fully sized, or live.
    # Rows are counted and stability-checked independently. In particular,
    # an entirely stop-free manually managed position can be observed.
    # The four-slot durable recovery gate still requires independent native
    # stop identity, state, price and protective-volume verification.
    if (
        set(read.private_risk_by_symbol) != set(symbols)
        or set(read.public_contract_by_symbol) != set(symbols)
        or set(read.fair_by_symbol) != set(symbols)
    ):
        raise ValueError("RISK_MARK_SYMBOL_SET_MISMATCH")
    for pos in read.positions:
        sym = pos["symbol"]
        fair = read.fair_by_symbol[sym]
        if (
            type(fair) is not dict
            or type(fair.get("observed_at_ms")) is not int
            or abs(fair["observed_at_ms"] - read.observed_at_ms) > max_age_ms
        ):
            raise ValueError("FAIR_MARK_UNDATED")
        marks[sym] = _positive(fair.get("fairPrice"))
        witness = assess_risk_tier_witness(
            symbol=sym, contracts=_positive(pos.get("holdVol")),
            private_risk={sym: read.private_risk_by_symbol[sym]},
            public_contract=read.public_contract_by_symbol[sym],
        )
        if witness.status != "CURRENT_CUSTOM_VOLUME_RISK_SCHEDULE_OBSERVED_NONAUTHORIZING":
            raise ValueError(
                witness.status if witness.status in SAFE_FRAME_FAILURE_CODES
                else "RISK_TIER_CURRENT_ONLY_UNVERIFIED"
            )
    return tuple(sorted(symbols)), marks, _static_hash(read)


async def observe_cross_risk_window(
    reader: CrossRiskReader, *, now_ms: Callable[[], int],
    max_age_ms: int = 2500, max_separation_ms: int = 2500,
    max_fair_drift_bps: Decimal = Decimal("100"),
) -> CrossRiskObservation:
    """No exchange POST; healthy result is evidence only, not position adoption."""
    def blocked(
        reason: str, stage: str = "NONE", code: str = "NONE",
    ) -> CrossRiskObservation:
        return CrossRiskObservation(
            status=reason, failure_stage=stage, failure_code=code,
        )

    def safe_error(error: Exception, *, read: bool) -> str:
        # Only static, audited constants from the collector and frame parser.
        allowed = (
            SAFE_COLLECTOR_FAILURE_CODES if read
            else SAFE_FRAME_FAILURE_CODES
        )
        raw = str(error)
        return raw if raw in allowed else "UNCLASSIFIED_READ_OR_EVIDENCE_FAILURE"

    if (
        type(max_age_ms) is not int or not 0 < max_age_ms <= 5000
        or type(max_separation_ms) is not int or not 0 < max_separation_ms <= 5000
    ):
        return blocked("CROSS_RISK_WINDOW_CONFIG_INVALID")
    try:
        drift_limit = _positive(max_fair_drift_bps)
        if drift_limit > Decimal("200"):
            return blocked("CROSS_RISK_WINDOW_DRIFT_LIMIT_INVALID")
    except Exception:
        return blocked("CROSS_RISK_WINDOW_DRIFT_LIMIT_INVALID")

    try:
        first = await reader.read()
    except Exception as exc:
        return blocked(
            "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED",
            "READ_FIRST", safe_error(exc, read=True),
        )
    try:
        syms1, marks1, sig1 = _frame(
            first, now_ms=now_ms(), max_age_ms=max_age_ms,
        )
    except Exception as exc:
        return blocked(
            "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED",
            "VALIDATE_FIRST", safe_error(exc, read=False),
        )
    try:
        second = await reader.read()
    except Exception as exc:
        return blocked(
            "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED",
            "READ_SECOND", safe_error(exc, read=True),
        )
    try:
        syms2, marks2, sig2 = _frame(
            second, now_ms=now_ms(), max_age_ms=max_age_ms,
        )
    except Exception as exc:
        return blocked(
            "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED",
            "VALIDATE_SECOND", safe_error(exc, read=False),
        )
    elapsed = second.observed_at_ms - first.observed_at_ms
    if elapsed <= 0 or elapsed > max_separation_ms:
        return blocked("CROSS_RISK_READ_WINDOW_NOT_CONSECUTIVE")
    if syms1 != syms2 or sig1 != sig2:
        return blocked("CROSS_RISK_CRITICAL_STATE_DRIFT")
    try:
        if any(
            abs(marks2[s] - marks1[s]) / marks1[s] * Decimal(10000)
            > drift_limit
            for s in syms1
        ):
            return blocked("CROSS_RISK_FAIR_PRICE_DRIFT")
    except (ArithmeticError, ValueError, KeyError):
        return blocked("CROSS_RISK_FAIR_PRICE_DRIFT_UNVERIFIED")
    return CrossRiskObservation(
        status="CURRENT_CROSS_RISK_OBSERVED_NONAUTHORIZING",
        observed_positions=len(syms2),
        observed_current_stops=len(second.stops),
        native_stop_protection_verified=False,
        stable_critical_inventory=True,
        position_margin_economic_witness_observed=True,
        current_risk_tiers_observed=True,
        fair_prices_observed=True,
        evidence_fingerprint_sha256=sig2,
    )

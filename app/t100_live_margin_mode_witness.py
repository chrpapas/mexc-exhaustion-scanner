"""Independent GET-only MEXC current short-side margin-mode corroboration.

Checks risk_limit and position/leverage (two reads each), for one requested
candidate symbol, with no orders, private POST, leverage change, DB or Render.
The leverage endpoint may omit symbol in its returned records; request scope
is established by the explicit GET query, not an asserted account-wide proof.
All output is redacted and nonauthorizing. No open positions are inspected or
adopted; current margin mode does not attest future orders.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.t100_live_readonly_probe import ReadOnlyMexcProbe

_SYMBOL = re.compile(r"[A-Z0-9]+_USDT")
_PATH = "/api/v1/private/position/leverage"
_RISK_KEYS = ("symbol", "positionType", "openType", "level", "maxVol",
              "maxLeverage", "mmr", "imr", "currentMmr", "leverage", "limitBySys")
_LEVERAGE_KEYS = ("symbol", "positionType", "openType", "leverage",
                  "currentMmr", "maxLeverageView")


@dataclass(frozen=True)
class MarginModeWitness:
    status: str
    risk_limit_mode: str = "NOT_CHECKED"
    leverage_endpoint_mode: str = "NOT_CHECKED"
    independent_mode_reads_agree: bool = False
    risk_limit_two_get_consistent: bool = False
    leverage_two_get_consistent: bool = False
    private_cross_mode_corrobated: bool = False
    candidate_contract_sizing_verified: bool = False
    account_wide_collateral_certified: bool = False
    position_ownership_verified: bool = False
    native_stop_protection_verified: bool = False
    venue_liquidation_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _kind(value: Any) -> str:
    if type(value) is int and value == 1:
        return "ISOLATED"
    if type(value) is int and value == 2:
        return "CROSS"
    return "UNKNOWN"


def _one_short(rows: Any, *, symbol: str, require_symbol: bool) -> dict[str, Any]:
    if type(rows) is not list or not 1 <= len(rows) <= 2:
        raise ValueError("BAD_ROWS")
    if any(type(r) is not dict for r in rows):
        raise ValueError("BAD_ROW")
    shorts = [r for r in rows if type(r.get("positionType")) is int
              and r["positionType"] == 2]
    if len(shorts) != 1:
        raise ValueError("SHORT_NOT_UNIQUE")
    if any(r.get("symbol", None) not in (None, symbol) for r in rows):
        raise ValueError("SYMBOL_MISMATCH")
    if require_symbol and any(r.get("symbol") != symbol for r in rows):
        raise ValueError("SYMBOL_UNVERIFIED")
    return shorts[0]


def _stable(first: dict[str, Any], second: dict[str, Any],
            keys: tuple[str, ...]) -> bool:
    def canonical(row: dict[str, Any]) -> str:
        return json.dumps({k: row.get(k) for k in keys},
                          sort_keys=True, separators=(",", ":"), allow_nan=False)
    return canonical(first) == canonical(second)


async def inspect_margin_mode(
    private: ReadOnlyMexcProbe, client: Any, symbol: str,
) -> MarginModeWitness:
    def blocked(status: str, **kwargs: Any) -> MarginModeWitness:
        return MarginModeWitness(status=status, **kwargs)

    if type(symbol) is not str or _SYMBOL.fullmatch(symbol) is None:
        return blocked("MARGIN_MODE_SYMBOL_INVALID")
    if not isinstance(private, ReadOnlyMexcProbe) or not callable(
        getattr(client, "_private_get", None)
    ):
        return blocked("MARGIN_MODE_CLIENT_INVALID")

    risk_rows: list[dict[str, Any]] = []
    leverage_rows: list[dict[str, Any]] = []
    # Interleave both signed GETs, making changed mode between pairs visible.
    for _ in range(2):
        try:
            scoped = await private.raw_account_risk_limits(symbol)
            if set(scoped) != {symbol}:
                return blocked("MARGIN_MODE_RISK_SCOPE_UNVERIFIED")
            risk_rows.append(_one_short(scoped[symbol], symbol=symbol,
                                        require_symbol=True))
        except Exception:
            return blocked("MARGIN_MODE_RISK_GET_OR_SCHEMA_FAILED")
        try:
            raw = await client._private_get(_PATH, {"symbol": symbol})
        except Exception:
            return blocked("MARGIN_MODE_LEVERAGE_GET_FAILED")
        try:
            leverage_rows.append(_one_short(raw, symbol=symbol,
                                            require_symbol=False))
        except (ValueError, TypeError):
            return blocked("MARGIN_MODE_LEVERAGE_SCHEMA_UNVERIFIED")

    try:
        risk_stable = _stable(risk_rows[0], risk_rows[1], _RISK_KEYS)
        lev_stable = _stable(leverage_rows[0], leverage_rows[1], _LEVERAGE_KEYS)
    except (ValueError, TypeError):
        return blocked("MARGIN_MODE_RESPONSE_SCHEMA_UNVERIFIED")
    if not risk_stable or not lev_stable:
        return blocked("MARGIN_MODE_CRITICAL_READ_DRIFT",
                       risk_limit_two_get_consistent=risk_stable,
                       leverage_two_get_consistent=lev_stable)
    risk_mode = _kind(risk_rows[1].get("openType"))
    lev_mode = _kind(leverage_rows[1].get("openType"))
    common = dict(
        risk_limit_mode=risk_mode, leverage_endpoint_mode=lev_mode,
        risk_limit_two_get_consistent=True,
        leverage_two_get_consistent=True,
    )
    if "UNKNOWN" in (risk_mode, lev_mode):
        return blocked("MARGIN_MODE_VALUE_UNVERIFIED", **common)
    if risk_mode != lev_mode:
        return blocked("MARGIN_MODE_ENDPOINT_DISAGREEMENT", **common)
    if risk_mode == "ISOLATED":
        return blocked(
            "MARGIN_MODE_CURRENT_ISOLATED_CORROBORATED_NONAUTHORIZING",
            independent_mode_reads_agree=True, **common,
        )
    return blocked(
        "MARGIN_MODE_CURRENT_CROSS_CORROBORATED_NONAUTHORIZING",
        independent_mode_reads_agree=True,
        private_cross_mode_corrobated=True, **common,
    )

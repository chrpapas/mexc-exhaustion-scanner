"""Offline T100 shadow fixture replay with fail-closed, redacted evidence.

Input is an iterable of already-decoded JSON-like observations. No venue
access, credentials, file writes, exchange mutations, or live authorization.
The fixture producer is responsible for redacting sensitive fields and
establishing pagination completeness independently.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any, Iterable

from app.t100_live_shadow_session import ShadowObservation, ShadowSession
from app.t100_live_supervisor import ManagedShort


class ReplayHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class ReplayEvidence:
    mode: str
    observations: int
    first_at_ms: int
    last_at_ms: int
    proposals: int
    input_sha256: str
    status: str


def _decimal(raw: Any) -> Decimal:
    if type(raw) is not str:
        raise ReplayHalted("INVALID_DECIMAL_FIELD")
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ReplayHalted("INVALID_DECIMAL_FIELD") from exc
    if not value.is_finite():
        raise ReplayHalted("INVALID_DECIMAL_FIELD")
    return value


def _int(raw: Any) -> int:
    if type(raw) is not int or raw < 0:
        raise ReplayHalted("INVALID_TIMESTAMP")
    return raw


def _observation(raw: Any) -> ShadowObservation:
    if not isinstance(raw, dict):
        raise ReplayHalted("INVALID_FIXTURE_RECORD")
    required = {"positions", "stops", "expected", "observed_at_ms", "now_ms",
                "positions_complete", "stops_complete"}
    optional = {"observed_lows", "price_ticks"}
    if not required <= raw.keys() or set(raw) - required - optional:
        raise ReplayHalted("FIXTURE_SCHEMA_MISMATCH")
    if type(raw["positions_complete"]) is not bool or type(raw["stops_complete"]) is not bool:
        raise ReplayHalted("INVALID_COMPLETENESS_FLAG")
    if not isinstance(raw["expected"], list):
        raise ReplayHalted("INVALID_EXPECTED_POSITIONS")
    managed = []
    fields = {"symbol", "position_id", "contracts", "entry_price",
              "stop_order_id", "stop_price", "best_profit_pct", "armed"}
    for row in raw["expected"]:
        if not isinstance(row, dict) or set(row) != fields:
            raise ReplayHalted("INVALID_MANAGED_POSITION_SCHEMA")
        if not all(type(row[k]) is str and row[k] for k in
                   ("symbol", "position_id", "stop_order_id")) or type(row["armed"]) is not bool:
            raise ReplayHalted("INVALID_MANAGED_POSITION_IDENTITY")
        managed.append(ManagedShort(
            row["symbol"], row["position_id"], _decimal(row["contracts"]),
            _decimal(row["entry_price"]), row["stop_order_id"],
            _decimal(row["stop_price"]), _decimal(row["best_profit_pct"]),
            row["armed"],
        ))
    lows = raw.get("observed_lows", {})
    ticks = raw.get("price_ticks", {})
    if not isinstance(lows, dict) or not isinstance(ticks, dict):
        raise ReplayHalted("INVALID_MARKET_DATA_SCHEMA")
    if any(type(k) is not str or not k for k in list(lows) + list(ticks)):
        raise ReplayHalted("INVALID_MARKET_DATA_SYMBOL")
    return ShadowObservation(
        positions=raw["positions"], stops=raw["stops"], expected=managed,
        observed_at_ms=_int(raw["observed_at_ms"]), now_ms=_int(raw["now_ms"]),
        positions_complete=raw["positions_complete"],
        stops_complete=raw["stops_complete"],
        observed_lows={k: _decimal(v) for k, v in lows.items()},
        price_ticks={k: _decimal(v) for k, v in ticks.items()},
    )


def replay_shadow_records(
    records: Iterable[dict[str, Any]], *, max_gap_ms: int = 60_000,
    max_records: int = 100_000,
) -> ReplayEvidence:
    if type(max_records) is not int or not 0 < max_records <= 100_000:
        raise ValueError("INVALID_MAX_RECORDS")
    session = ShadowSession(max_gap_ms=max_gap_ms)
    digest = sha256()
    count = proposals = 0
    first_at = last_at = None
    try:
        for raw in records:
            if count >= max_records:
                raise ReplayHalted("FIXTURE_RECORD_LIMIT_EXCEEDED")
            # Canonical digest of input; only digest, not raw account data,
            # appears in the evidence report.
            serialized = json.dumps(raw, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True, allow_nan=False)
            digest.update(serialized.encode("utf-8") + b"\n")
            observation = _observation(raw)
            report = session.observe(observation)
            if first_at is None:
                first_at = report.latest_at_ms
            last_at = report.latest_at_ms
            proposals += len(report.last_result.proposals)
            count += 1
    except Exception as exc:
        raise ReplayHalted(f"SHADOW_REPLAY_FAILED_AT_RECORD_{count + 1}") from exc
    if not count:
        raise ReplayHalted("EMPTY_SHADOW_REPLAY")
    return ReplayEvidence(
        "SHADOW_ONLY", count, first_at, last_at, proposals,
        digest.hexdigest(), "OFFLINE_FIXTURE_REPLAY_ONLY",
    )

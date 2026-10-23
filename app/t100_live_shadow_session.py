"""Fail-closed sequential T100 shadow audit. No network, orders, or recovery.

A process-wide shadow session must be recreated only after operator review
if any observation fails. This module intentionally has no reset method.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

from app.t100_live_shadow import ShadowResult, shadow_reconcile
from app.t100_live_supervisor import ManagedShort


class ShadowSessionHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class ShadowObservation:
    positions: Any
    stops: Any
    expected: Sequence[ManagedShort]
    observed_at_ms: int
    now_ms: int
    positions_complete: bool
    stops_complete: bool
    observed_lows: dict[str, Decimal] | None = None
    price_ticks: dict[str, Decimal] | None = None


@dataclass(frozen=True)
class ShadowSessionReport:
    observations: int
    latest_at_ms: int
    last_result: ShadowResult
    mode: str = "SHADOW_ONLY"


class ShadowSession:
    """Sequence guard around independently complete exchange observations.

    Timestamps are strictly increasing and bounded by the configured gap.
    No session can resume after a failure. A successful report is not
    permission to trade, nor proof of continuous exchange availability.
    """

    def __init__(self, *, max_gap_ms: int = 60_000) -> None:
        if not isinstance(max_gap_ms, int) or isinstance(max_gap_ms, bool) or not 0 < max_gap_ms <= 300_000:
            raise ValueError("INVALID_MAX_SHADOW_GAP")
        self.max_gap_ms = max_gap_ms
        self._halted = False
        self._last_at: int | None = None
        self._observations = 0

    @property
    def halted(self) -> bool:
        return self._halted

    def observe(self, item: ShadowObservation) -> ShadowSessionReport:
        if self._halted:
            raise ShadowSessionHalted("SHADOW_SESSION_PERMANENTLY_HALTED")
        try:
            if not isinstance(item.observed_at_ms, int) or isinstance(item.observed_at_ms, bool):
                raise ShadowSessionHalted("INVALID_SHADOW_TIMESTAMP")
            if self._last_at is not None:
                if item.observed_at_ms <= self._last_at:
                    raise ShadowSessionHalted("NONMONOTONIC_SHADOW_TIMESTAMP")
                if item.observed_at_ms - self._last_at > self.max_gap_ms:
                    raise ShadowSessionHalted("SHADOW_OBSERVATION_GAP")
            result = shadow_reconcile(
                positions=item.positions,
                stops=item.stops,
                expected=item.expected,
                observed_at_ms=item.observed_at_ms,
                now_ms=item.now_ms,
                positions_complete=item.positions_complete,
                stops_complete=item.stops_complete,
                observed_lows=item.observed_lows,
                price_ticks=item.price_ticks,
            )
        except Exception as exc:
            self._halted = True
            raise ShadowSessionHalted("SHADOW_AUDIT_FAILED") from exc
        self._last_at = item.observed_at_ms
        self._observations += 1
        return ShadowSessionReport(self._observations, item.observed_at_ms, result)

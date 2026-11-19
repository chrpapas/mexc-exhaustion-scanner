"""Read-only, signal-specific Stage2 -> four-slot handoff identity.

This makes a source candidate's exact immutable identity available to the
portfolio runtime without creating an order context, a permit or a POST path.
It deliberately shares the coordinator's frozen-priority selector: no later
candidate may replace the first unheld candidate, even when multiple Stage2
rows exist. The real production executor must still independently re-read
this signal, inspect exact lots/margin, issue its own certified permit, and
construct the full order/stop/exit graph.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
from app.t100_live_v40_entry_coordinator import (
    select_frozen_portfolio_pinned_candidate,
)
from app.t100_live_v40_stage2_source import (
    MAX_SIGNAL_AGE_SECONDS,
    V40SourceBatch,
    V40SourceCandidate,
)
from app.t100_v40_admission import VOL_SKIP_THRESHOLD


class V40SignalBindingHalted(ValueError):
    """Invalid or ambiguous source/slot state; never authorize an entry."""


@dataclass(frozen=True)
class V40ReadOnlySignalIdentity:
    signal_id: int
    symbol: str
    tier: str
    admission_open_ts: int
    observed_age_seconds: float
    vol30_passed: bool
    # This is only a typed, read-only identity for later rechecking.
    entry_allowed: bool = False
    live_authorization: bool = False


def bind_first_unheld_stage2_identity(
    source: V40SourceBatch,
    snapshot: FourSlotSnapshotResult,
) -> V40ReadOnlySignalIdentity | None:
    """Return one exact fresh signal identity (or no candidate), without I/O.

    False source readiness, a malformed ledger/account snapshot, duplicate
    IDs/symbols, altered ordering, or a stale selected signal are all hard
    stops. Occupied symbols are never submitted again, and no candidate is
    reordered based on venue prices or convenience.
    """
    if not (
        type(source) is V40SourceBatch
        and source.status == "FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING"
        and source.read_only_repeatable_read is True
        and source.history_complete is True
        and source.entry_allowed is False
        and source.live_authorization is False
        and type(source.candidates) is tuple
        and all(type(x) is V40SourceCandidate for x in source.candidates)
        and source.candidates == tuple(sorted(
            source.candidates, key=lambda c: (c.admission_open_ts, c.signal_id),
        ))
        and len({x.signal_id for x in source.candidates}) == len(source.candidates)
        and len({x.symbol for x in source.candidates}) == len(source.candidates)
        and type(snapshot) is FourSlotSnapshotResult
        and snapshot.status in (
            "FLAT_OBSERVED_NONAUTHORIZING",
            "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
        )
        and snapshot.entry_allowed is False
        and snapshot.live_authorization is False
        and type(snapshot.occupied_slots) is int
        and 0 <= snapshot.occupied_slots <= 4
    ):
        raise V40SignalBindingHalted("V40_SOURCE_OR_SNAPSHOT_INVALID")
    if snapshot.status == "FLAT_OBSERVED_NONAUTHORIZING":
        if snapshot.occupied_slots != 0 or snapshot.recovery is not None:
            raise V40SignalBindingHalted("V40_FLAT_SNAPSHOT_TOPOLOGY_INVALID")
        held: set[str] = set()
    else:
        if snapshot.recovery is None:
            raise V40SignalBindingHalted("V40_NONFLAT_RECOVERY_MISSING")
        positions = tuple(snapshot.recovery.recovered)
        held = {x.symbol for x in positions}
        if (len(positions) != snapshot.occupied_slots
            or len(held) != len(positions)
            or len({x.position_id for x in positions}) != len(positions)):
            raise V40SignalBindingHalted("V40_NONFLAT_RECOVERY_INVALID")
    if snapshot.occupied_slots == 4:
        return None
    unheld = tuple(x for x in source.candidates if x.symbol not in held)
    if not unheld:
        return None
    candidate = unheld[0]
    if not (
        type(candidate.signal_id) is int and candidate.signal_id > 0
        and candidate.tier in ("LOW_100", "HIGH_225")
        and type(candidate.observed_age_seconds) in (int, float)
        and isfinite(candidate.observed_age_seconds)
        and 0 <= candidate.observed_age_seconds <= MAX_SIGNAL_AGE_SECONDS
        and type(candidate.pre24_vol_pct) in (int, float)
        and isfinite(candidate.pre24_vol_pct)
        and candidate.pre24_vol_pct >= 0
        and candidate.entry_allowed is False
        and candidate.live_authorization is False
    ):
        raise V40SignalBindingHalted("V40_PINNED_CANDIDATE_NOT_FRESH")
    try:
        selected = select_frozen_portfolio_pinned_candidate(
            source, snapshot,
            signal_id=candidate.signal_id,
            symbol=candidate.symbol,
            tier=candidate.tier,
        )
    except Exception:
        raise V40SignalBindingHalted("V40_FROZEN_PRIORITY_HANDOFF_FAILED") from None
    if selected is not candidate:
        raise V40SignalBindingHalted("V40_FROZEN_CANDIDATE_IDENTITY_CHANGED")
    return V40ReadOnlySignalIdentity(
        signal_id=selected.signal_id,
        symbol=selected.symbol,
        tier=selected.tier,
        admission_open_ts=selected.admission_open_ts,
        observed_age_seconds=selected.observed_age_seconds,
        vol30_passed=selected.pre24_vol_pct < VOL_SKIP_THRESHOLD,
    )

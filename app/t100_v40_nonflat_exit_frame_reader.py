"""Compose real 1–4 owned V40 positions and completed Min30 bars, GET-only.

This is the missing nonflat observation path for the disabled Render worker.
Requires coordinator-proven current owned exchange/native SLs, exact
unchanged replay ledger hash, separately selected 30-minute scanner bars
with durable cursor, current public ticks, unchanged bookends after reads,
fresh independent nonflat guardian and genuine Stage2 source observation.

A candle touch is only an exit *candidate*: NEVER call a native close
trigger proven, amend a stop, release slot or issue a live permit here.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime,timezone
from typing import Any

from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult,_ledger_digest,_venue_digest,
)
from app.t100_live_four_slot_mexc_readonly import FourSlotMexcReadVenue
from app.t100_v40_owned_completed_bar_reader import (
    PostgresV40OwnedBarReader,V40OwnedDueBarWindow,
)
from app.t100_v40_guardian_post_gate import V40GuardianPostGateEvidence
from app.t100_v40_unreleased_nonflat_guardian_evidence import (
    V40NonflatGuardianEvidence,
)
from app.t100_v40_unreleased_verified_exit_handoff import (
    project_verified_owned_v40_exits,
)
from app.t100_v40_live_portfolio_supervisor import V40PortfolioCycleFrame

GUARDIAN_GOOD="V40_NONFLAT_GUARDIAN_PROTECTED_OBSERVER_FRESH_NONAUTHORIZING"


class V40NonflatFrameHalted(RuntimeError):
    """Never include exchange responses or SQL error messages."""


class V40RealNonflatExitFrameReader:
    def __init__(
        self,*,owned_bars:PostgresV40OwnedBarReader,
        nonflat_guardian:Any,
    )->None:
        if (
            type(owned_bars) is not PostgresV40OwnedBarReader
            or not callable(getattr(nonflat_guardian,"verify",None))
        ):
            raise V40NonflatFrameHalted("V40_NONFLAT_FRAME_DEPENDENCIES_INVALID")
        self.owned_bars=owned_bars
        self.nonflat_guardian=nonflat_guardian

    async def observe(
        self,*,snap:FourSlotSnapshotResult,venue:FourSlotMexcReadVenue,
        ledger:Any,source:Any,observed_at:datetime,
    )->V40PortfolioCycleFrame:
        if (
            type(snap) is not FourSlotSnapshotResult
            or snap.status!="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"
            or not 1<=snap.occupied_slots<=4
            or snap.recovery is None
            or not snap.ledger_hash or not snap.critical_inventory_hash
            or type(venue) is not FourSlotMexcReadVenue
            or not callable(getattr(ledger,"read_snapshot",None))
            or not callable(getattr(source,"latest_candidates",None))
            or type(observed_at) is not datetime
            or observed_at.tzinfo is None or observed_at.utcoffset() is None
        ):
            raise V40NonflatFrameHalted("V40_NONFLAT_FRAME_SOURCE_OR_SNAPSHOT_INVALID")
        managed=snap.recovery.recovered
        if (
            len(managed)!=snap.occupied_slots
            or len({m.position_id for m in managed})!=len(managed)
            or len({m.symbol for m in managed})!=len(managed)
        ):
            raise V40NonflatFrameHalted("V40_NONFLAT_FRAME_MANAGED_IDENTITIES_INVALID")
        try:
            # Venue remembers the coordinator's AFTER bookend; no extra
            # account state is inferred from a scanner signal.
            current_positions=deepcopy(venue.last_positions)
            current_stops=deepcopy(venue.last_stops)
            first=await ledger.read_snapshot()
            if (
                _ledger_digest(first)!=snap.ledger_hash
                or _venue_digest(current_positions,current_stops)
                    !=snap.critical_inventory_hash
            ):
                raise V40NonflatFrameHalted("V40_NONFLAT_FRAME_INITIAL_BOOKEND_DRIFT")
            bars=await self.owned_bars.observe(
                position_ids=tuple(m.position_id for m in managed),
                symbol_by_position={m.position_id:m.symbol for m in managed},
                now_ts=int(observed_at.timestamp()),
            )
            if (
                type(bars) is not V40OwnedDueBarWindow
                or bars.status!="V40_OWNED_MIN30_DUE_CANDLES_VERIFIED_NONAUTHORIZING"
                or bars.runtime_role_verified is not True
                or bars.exchange_public_specs_observed is not True
                or bars.next_entry_allowed is not False
                or bars.live_authorization is not False
                or bars.mexc_posts!=0 or bars.database_writes!=0
                or len(bars.cursors)!=len(managed)
                or {c.position_id for c in bars.cursors}
                    !={m.position_id for m in managed}
                or len(bars.bars)+len(bars.no_due_position_ids)!=len(managed)
            ):
                raise V40NonflatFrameHalted("V40_NONFLAT_FRAME_DUE_BAR_SOURCE_INVALID")
            projection=(
                project_verified_owned_v40_exits(
                    snapshot=snap,ledger=first,
                    signed_account_positions=current_positions,
                    signed_native_stops=current_stops,
                    cursors=bars.cursors,
                    completed_bars_by_symbol=bars.bars,
                    price_ticks_by_symbol=bars.ticks,
                    now_ts=int(observed_at.timestamp()),
                )
                if bars.bars else None
            )
            # Independently recheck every raw exchange/ledger bookend AFTER
            # scanner SQL/cursor and public tick reads.
            end_positions=await venue.open_positions()
            end_stops=await venue.open_stop_orders()
            second=await ledger.read_snapshot()
            if (
                _ledger_digest(second)!=snap.ledger_hash
                or _venue_digest(end_positions,end_stops)
                    !=snap.critical_inventory_hash
            ):
                raise V40NonflatFrameHalted("V40_NONFLAT_FRAME_FINAL_BOOKEND_DRIFT")
            guardian=await self.nonflat_guardian.verify()
            if (
                type(guardian) is not V40NonflatGuardianEvidence
                or guardian.status!=GUARDIAN_GOOD
                or guardian.guardian_protected_observation_fresh is not True
                or guardian.no_current_scope_critical_or_pending_incidents is not True
                or guardian.next_entry_allowed is not False
                or guardian.live_authorization is not False
                or guardian.exchange_posts!=0
                or guardian.database_writes!=0
            ):
                raise V40NonflatFrameHalted("V40_NONFLAT_FRAME_GUARDIAN_LATCH_OR_FRESHNESS")
            batch=await source.latest_candidates(observed_at=observed_at)
            return V40PortfolioCycleFrame(
                snapshot=snap,guardian=guardian,source=batch,
                completed_exit=projection,
                no_bar_due_ids=bars.no_due_position_ids,
            )
        except V40NonflatFrameHalted:
            raise
        except Exception:
            raise V40NonflatFrameHalted("V40_NONFLAT_FRAME_READ_OR_PROJECTION_FAILED") from None

"""GET-only positive native exit recovery when the account has one fewer slot.

The ordinary four-slot snapshot correctly refuses an OPEN checkpoint whose
MEXC position is missing. That refusal MUST NOT prevent native-stop terminal
reconciliation: the actual stop can execute between Min30 bars and before SQL
checkpoint closure. Reconciliation starts from the immutable OPEN ledger and
fresh independent full-account GET bookends. No candle/price touch is required.

This is evidence, never an UPDATE, order permit, accounting settlement, or
automatic slot release. Downstream release must separately prove immutable
contract economics, funding, a current leader fence and an atomic DB close.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from app.t100_live_durable_recovery import (
    PositionCheckpoint,checkpoint_from_row,
)
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_mexc_readonly import FourSlotMexcReadVenue
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult,_ledger_digest,_venue_digest,
)
from app.t100_live_owned_emergency_guardian import OwnedShort
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_v40_unreleased_portfolio_native_close import (
    V40PortfolioCloseWitness,observe_one_native_exit_among_v40_survivors,
)


class V40NativeCloseMismatchHalted(RuntimeError):
    """No raw SQL, GET payloads, account identifiers or credentials."""


@dataclass(frozen=True)
class V40NativeCloseMismatchEvidence:
    status: str
    previous: PositionCheckpoint
    survivors: tuple[PositionCheckpoint,...]
    witness: V40PortfolioCloseWitness
    ledger_bookends_equal: bool = True
    venue_bookends_equal: bool = True
    positive_native_close_verified: bool = True
    funding_and_final_accounting_verified: bool = False
    slot_release_permitted: bool = False
    next_entry_allowed: bool = False
    live_authorization: bool = False
    mexc_posts: int = 0
    database_writes: int = 0


class V40NativeCloseMismatchRecovery:
    """Uses the exact same signed MEXC probe as the main portfolio venue."""

    def __init__(
        self, *, ledger: PostgresFourSlotLedgerReader,
        venue: FourSlotMexcReadVenue,probe: ReadOnlyMexcProbe,
        independently_verified_account_scope_sha256: str,
    )->None:
        if (
            type(ledger) is not PostgresFourSlotLedgerReader
            or type(venue) is not FourSlotMexcReadVenue
            or type(probe) is not ReadOnlyMexcProbe
            or venue.probe is not probe
            or type(independently_verified_account_scope_sha256) is not str
            or re.fullmatch(
                r"[a-f0-9]{64}",independently_verified_account_scope_sha256
            ) is None
        ):
            raise V40NativeCloseMismatchHalted(
                "V40_MISSING_CLOSE_SAME_ACCOUNT_LEDGER_AND_PROBE_REQUIRED"
            )
        self.ledger=ledger
        self.venue=venue
        self.probe=probe
        self.scope=independently_verified_account_scope_sha256

    async def observe(
        self, snapshot:FourSlotSnapshotResult,
    )->V40NativeCloseMismatchEvidence:
        if (
            type(snapshot) is not FourSlotSnapshotResult
            or snapshot.status!="UNOWNED_OR_MISSING_CHECKPOINTS"
            or snapshot.entry_allowed is not False
            or snapshot.live_authorization is not False
        ):
            raise V40NativeCloseMismatchHalted(
                "V40_MISSING_CLOSE_EXACT_SNAPSHOT_MISMATCH_REQUIRED"
            )
        try:
            first=await self.ledger.read_snapshot()
            digest=_ledger_digest(first)
            checkpoints=tuple(
                checkpoint_from_row(row) for row in first.checkpoints
            )
            if (
                not 1<=len(checkpoints)<=4
                or any(cp.lifecycle_state!="OPEN" for cp in checkpoints)
                or len({cp.position_id for cp in checkpoints})!=len(checkpoints)
                or len({cp.symbol for cp in checkpoints})!=len(checkpoints)
            ):
                raise V40NativeCloseMismatchHalted(
                    "V40_MISSING_CLOSE_IMMUTABLE_OPEN_LEDGER_UNVERIFIED"
                )
            positions=await self.venue.open_positions()
            stops=await self.venue.open_stop_orders()
            if (
                type(positions) is not list or type(stops) is not list
                or len(positions)!=len(checkpoints)-1
                or len(stops)!=len(positions)
                or any(type(row) is not dict for row in positions+stops)
            ):
                raise V40NativeCloseMismatchHalted(
                    "V40_MISSING_CLOSE_EXACT_ONE_POSITION_AND_STOP_REQUIRED"
                )
            venue_digest=_venue_digest(positions,stops)
            live_ids=[
                str(x.get("positionId") or "") for x in positions
            ]
            stop_ids=[
                str(x.get("positionId") or "") for x in stops
            ]
            ids={cp.position_id for cp in checkpoints}
            live=set(live_ids)
            if (
                len(live)!=len(live_ids)
                or len(set(stop_ids))!=len(stop_ids)
                or set(stop_ids)!=live
                or not live.issubset(ids)
                or len(ids-live)!=1
            ):
                raise V40NativeCloseMismatchHalted(
                    "V40_MISSING_CLOSE_FOREIGN_OR_NONUNIQUE_POSITION"
                )
            closing=next(cp for cp in checkpoints if cp.position_id not in live)
            survivors=tuple(cp for cp in checkpoints if cp.position_id in live)
            owned=OwnedShort(
                scope_sha256=self.scope,symbol=closing.symbol,
                position_id=closing.position_id,
                entry_external_oid=closing.external_oid,
                entry_order_id=closing.exchange_order_id,
                contracts=closing.contracts,
                stop_order_id=closing.stop_order_id,
                stop_price=closing.stop_price,
                checkpoint_version=closing.checkpoint_version,
            )
            witness=await observe_one_native_exit_among_v40_survivors(
                probe=self.probe,closing=owned,
                account_scope_sha256=self.scope,surviving=survivors,
            )
            # The native-close observer *independently* verifies full venue
            # count/stop/terminal/close-fill and survivor bookends. Require a
            # further ledger and account bookend; any second closure in the
            # same interval MUST halt rather than infer two free slots.
            after_positions=await self.venue.open_positions()
            after_stops=await self.venue.open_stop_orders()
            second=await self.ledger.read_snapshot()
            if (
                _ledger_digest(second)!=digest
                or _venue_digest(after_positions,after_stops)!=venue_digest
            ):
                raise V40NativeCloseMismatchHalted(
                    "V40_MISSING_CLOSE_LEDGER_OR_ACCOUNT_CHANGED_DURING_GET"
                )
            if (
                type(witness) is not V40PortfolioCloseWitness
                or witness.status not in (
                    "V40_PORTFOLIO_CLOSE_EXCHANGE_TP_LABEL_CONFLICT_UNRELEASED",
                    "V40_PORTFOLIO_CLOSE_EXCHANGE_SL_LABEL_OBSERVED_UNRELEASED",
                )
                or witness.closed_position_id!=closing.position_id
                or witness.surviving_protected_slots!=len(survivors)
                or witness.exact_closed_position_history_verified is not True
                or witness.portfolio_unchanged_across_bookends is not True
                or witness.closing_fills is None
                or witness.native_terminal is None
                or witness.native_terminal.native_stop_terminal_verified is not True
                or witness.native_terminal.linked_venue_close_filled is not True
                or witness.native_terminal.owned_position_closed_verified is not True
                or witness.closing_fills.position_id!=closing.position_id
                or witness.closing_fills.symbol!=closing.symbol
                or witness.closing_fills.side!="CLOSE_SHORT"
                or witness.closing_fills.deal_volume!=closing.contracts
                or witness.closing_fills.final_order_fill_history_complete is not True
                or witness.closing_fills.strategy_fully_filled_order_complete is not True
                or witness.slot_released is not False
                or witness.live_authorization is not False
                or witness.mexc_posts!=0 or witness.database_writes!=0
            ):
                raise V40NativeCloseMismatchHalted(
                    "V40_MISSING_CLOSE_NATIVE_TERMINAL_OR_FILLS_NOT_POSITIVE"
                )
            return V40NativeCloseMismatchEvidence(
                status="V40_EXACT_ONE_NATIVE_EXIT_POSITIVE_ACCOUNTING_AND_RELEASE_PENDING",
                previous=closing,survivors=survivors,witness=witness,
            )
        except V40NativeCloseMismatchHalted:
            raise
        except BaseException:
            raise V40NativeCloseMismatchHalted(
                "V40_MISSING_CLOSE_SIGNED_GET_OR_LEDGER_UNVERIFIED"
            ) from None

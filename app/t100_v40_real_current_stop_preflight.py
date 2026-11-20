"""Concrete signed-GET, closed-Min30 per-position V40 trailing preflight.

Recomputes the frozen short trailing-stop decision independently of an
earlier supervisor projection, using restricted immutable checkpoints,
the causal last fully CLOSED 30-minute bar, current public last price,
and two complete signed MEXC position/native-SL inventory bookends.

Checkpoint.best_profit_pct stores the previously verified cumulative
maximum short profit. Its equivalent historical low is reconstructed from
the *exact immutable entry price*. Taking the minimum with the newly
completed bar low preserves the 14% activation / 0.5pp gap rule without
substituting an unrelated trade, ticker, current candle, or intrabar guess.

This is a GET-only *witness* for the existing stop POST adapter. It never
submits an order, changes a stop, writes SQL or grants a permit. If the
signal ID, contract multiplier, active market stop, causal bar, MEXC market
ticker or full-account bookends cannot be verified, it halts closed.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D, InvalidOperation
from typing import Any,Callable

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_durable_recovery import (
    PositionCheckpoint,checkpoint_from_row,
)
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_mexc_readonly import (
    FourSlotMexcReadVenue,FourSlotMexcCompleteness,
)
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotCoordinator,_ledger_digest,_venue_digest,
)
from app.t100_v40_owned_completed_bar_reader import (
    PostgresV40OwnedBarReader,V40OwnedDueBarWindow,
)
from app.t100_live_trailing_preflight import (
    TrailingBookend,TrailingProposalReview,review_t100_trailing_candidate,
)
from app.t100_v40_portfolio_stop_supervisor_port import V40LiveStopReadback
from app.t100_v40_unreleased_exit_policy import (
    V40CompletedBar,propose_next_completed_bar,V40ExitCheckpoint,
)


class V40RealStopPreflightHalted(RuntimeError):
    """No secret, raw SQL, account row, or API error in any message."""


class V40RealCurrentStopPreflight:
    def __init__(
        self, *,ledger:PostgresFourSlotLedgerReader,
        snapshots:FourSlotSnapshotCoordinator,
        venue:FourSlotMexcReadVenue,
        completeness:FourSlotMexcCompleteness,
        owned_bars:PostgresV40OwnedBarReader,
        public_get:Any,
        now_ms:Callable[[],int],
    )->None:
        if (
            type(ledger) is not PostgresFourSlotLedgerReader
            or type(snapshots) is not FourSlotSnapshotCoordinator
            or type(venue) is not FourSlotMexcReadVenue
            or type(completeness) is not FourSlotMexcCompleteness
            or type(owned_bars) is not PostgresV40OwnedBarReader
            or snapshots.ledger is not ledger
            or snapshots.venue is not venue
            or snapshots.completeness is not completeness
            or completeness.venue is not venue
            or owned_bars.runtime_pool is not ledger.pool
            or not callable(getattr(public_get,"get",None))
            or owned_bars.public_get is not public_get
            or not callable(now_ms)
        ):
            raise V40RealStopPreflightHalted("V40_CURRENT_STOP_ACCOUNT_OR_LEDGER_BINDING_INVALID")
        self.ledger=ledger
        self.snapshots=snapshots
        self.venue=venue
        self.completeness=completeness
        self.owned_bars=owned_bars
        self.public_get=public_get
        self.now_ms=now_ms

    async def _ticker(self,symbol:str)->tuple[D,int]:
        try:
            response=await self.public_get.get(
                "/api/v1/contract/ticker",params={"symbol":symbol},
            )
            response.raise_for_status()
            raw=response.json()
            if (
                type(raw) is not dict or raw.get("success") is not True
                or raw.get("code")!=0 or type(raw.get("data")) is not dict
                or raw["data"].get("symbol")!=symbol
                or type(raw["data"].get("timestamp")) is not int
            ):
                raise ValueError()
            price=D(str(raw["data"]["lastPrice"]))
            if not price.is_finite() or price<=0:
                raise ValueError()
            timestamp=raw["data"]["timestamp"]
            now=self.now_ms()
            if (
                type(now) is not int or now<=0
                or not 0<=now-timestamp<=5000
            ):
                raise ValueError()
            return price,timestamp
        except BaseException:
            raise V40RealStopPreflightHalted(
                "V40_CURRENT_STOP_PUBLIC_LAST_TRADE_UNVERIFIED"
            ) from None

    async def _bookend(self)->TrailingBookend:
        positions=await self.venue.open_positions()
        stops=await self.venue.open_stop_orders()
        counts=await self.completeness.inspect()
        t=self.now_ms()
        if (
            type(positions) is not list or type(stops) is not list
            or type(counts) is not dict or type(t) is not int or t<=0
            or any(type(x) is not dict for x in positions+stops)
        ):
            raise V40RealStopPreflightHalted(
                "V40_CURRENT_STOP_SIGNED_BOOKEND_SHAPE_INVALID"
            )
        return TrailingBookend(
            positions=deepcopy(positions),stops=deepcopy(stops),
            completeness=deepcopy(counts),observed_at_ms=t,
        )

    async def _owned_signal_id(self,cp:PositionCheckpoint)->int:
        try:
            async with self.ledger.pool.acquire() as conn:
                async with conn.transaction(
                    isolation="repeatable_read",readonly=True,
                ):
                    if (
                        await conn.fetchval("SELECT current_user")!=TARGET_ROLE
                        or await conn.fetchval("SELECT current_database()")!=EXPECTED_DATABASE
                        or await conn.fetchval("SELECT current_setting('transaction_read_only')")!="on"
                    ):
                        raise ValueError()
                    rows=await conn.fetch(
                        """SELECT signal_id FROM public.t100_live_v40_slot_reservations
                           WHERE external_oid=$1 AND symbol=$2 AND state='PROTECTED'
                           LIMIT 2""",cp.external_oid,cp.symbol,
                    )
                    if (
                        type(rows) not in (list,tuple) or len(rows)!=1
                        or type(rows[0]["signal_id"]) is not int
                        or rows[0]["signal_id"]<=0
                    ):
                        raise ValueError()
                    return rows[0]["signal_id"]
        except BaseException:
            raise V40RealStopPreflightHalted(
                "V40_CURRENT_STOP_PROTECTED_SIGNAL_ID_UNVERIFIED"
            ) from None

    async def observe(self,position_id:str)->V40LiveStopReadback:
        if (
            type(position_id) is not str or not position_id.isascii()
            or not position_id.isdecimal() or position_id.startswith("0")
        ):
            raise V40RealStopPreflightHalted("V40_CURRENT_STOP_POSITION_ID_INVALID")
        try:
            snapshot=await self.snapshots.inspect()
            if (
                snapshot.status!="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"
                or not 1<=snapshot.occupied_slots<=4
                or snapshot.recovery is None
                or snapshot.entry_allowed is not False
                or snapshot.live_authorization is not False
            ):
                raise V40RealStopPreflightHalted(
                    "V40_CURRENT_STOP_OWNED_POSITIONS_NOT_VERIFIED"
                )
            ledger=await self.ledger.read_snapshot()
            if _ledger_digest(ledger)!=snapshot.ledger_hash:
                raise V40RealStopPreflightHalted(
                    "V40_CURRENT_STOP_IMMUTABLE_LEDGER_CHANGED"
                )
            checkpoints=tuple(
                checkpoint_from_row(row) for row in ledger.checkpoints
            )
            if (
                len(checkpoints)!=snapshot.occupied_slots
                or len({c.position_id for c in checkpoints})!=len(checkpoints)
                or len({c.symbol for c in checkpoints})!=len(checkpoints)
                or len([c for c in checkpoints if c.position_id==position_id])!=1
                or any(c.lifecycle_state!="OPEN" for c in checkpoints)
            ):
                raise V40RealStopPreflightHalted(
                    "V40_CURRENT_STOP_CHECKPOINT_SCOPE_UNVERIFIED"
                )
            cp=next(c for c in checkpoints if c.position_id==position_id)
            now=self.now_ms()
            if type(now) is not int or now<=0:
                raise V40RealStopPreflightHalted("V40_CURRENT_STOP_CLOCK_INVALID")
            window=await self.owned_bars.observe(
                position_ids=tuple(c.position_id for c in checkpoints),
                symbol_by_position={c.position_id:c.symbol for c in checkpoints},
                now_ts=now//1000,
            )
            if (
                type(window) is not V40OwnedDueBarWindow
                or window.status!="V40_OWNED_MIN30_DUE_CANDLES_VERIFIED_NONAUTHORIZING"
                or window.runtime_role_verified is not True
                or window.scanner_role_verified is not True
                or window.exchange_public_specs_observed is not True
                or window.live_authorization is not False
                or window.mexc_posts!=0 or window.database_writes!=0
                or position_id in window.no_due_position_ids
                or cp.symbol not in window.bars or cp.symbol not in window.ticks
                or type(window.bars[cp.symbol]) is not V40CompletedBar
            ):
                raise V40RealStopPreflightHalted(
                    "V40_CURRENT_STOP_CAUSAL_COMPLETED_BAR_MISSING"
                )
            bar=window.bars[cp.symbol]
            tick=window.ticks[cp.symbol]
            matching_cursor=tuple(
                c for c in window.cursors if c.position_id==position_id
            )
            if (
                len(matching_cursor)!=1
                or matching_cursor[0].pending_open_ts is not None
                or matching_cursor[0].entry_external_oid!=cp.external_oid
                or matching_cursor[0].checkpoint_version!=cp.checkpoint_version
                or matching_cursor[0].confirmed_stop_price!=cp.stop_price
                or matching_cursor[0].best_profit_pct!=cp.best_profit_pct
                or matching_cursor[0].trail_armed is not cp.armed
            ):
                raise V40RealStopPreflightHalted(
                    "V40_CURRENT_STOP_CURSOR_OR_CHECKPOINT_DRIFT"
                )
            cursor=matching_cursor[0]
            # Recompute the frozen policy directly from the immutable
            # cursor/position and the exact closed candle. No live candle.
            projected=propose_next_completed_bar(
                V40ExitCheckpoint(
                    position_id=cp.position_id,symbol=cp.symbol,
                    entry_price=cp.entry_price,
                    filled_at_ts=cursor.signed_fill_at_ms//1000,
                    native_stop_price=cp.stop_price,
                    best_profit_pct=cp.best_profit_pct,
                    trail_armed=cp.armed,
                    last_completed_open_ts=cursor.last_committed_open_ts,
                ),bar,price_tick=tick,now_ts=now//1000,
            )
            if (
                projected.status!="NATIVE_STOP_TIGHTENING_PROPOSED_UNAUTHORIZED"
                or projected.proposed_checkpoint is None
                or projected.proposed_native_stop_price is None
            ):
                raise V40RealStopPreflightHalted(
                    "V40_CURRENT_STOP_FROZEN_BAR_NOT_TIGHTENING"
                )
            before=await self._bookend()
            current_last,market_ms=await self._ticker(cp.symbol)
            after=await self._bookend()
            after_ledger=await self.ledger.read_snapshot()
            if (
                _ledger_digest(after_ledger)!=snapshot.ledger_hash
                or _venue_digest(before.positions,before.stops)!=snapshot.critical_inventory_hash
                or _venue_digest(after.positions,after.stops)!=snapshot.critical_inventory_hash
            ):
                raise V40RealStopPreflightHalted(
                    "V40_CURRENT_STOP_ACCOUNT_OR_CHECKPOINT_CHANGED"
                )
            # A checkpoint's cumulative best gain is the durable summary
            # of all prior closed bars; preserve it and add the new causal
            # *completed* bar's low, never an unclosed/intrabar quote.
            historical_low=cp.entry_price*(D("1")-cp.best_profit_pct/D("100"))
            low_since_entry=min(historical_low,bar.low)
            reviewed=review_t100_trailing_candidate(
                checkpoints=list(checkpoints),
                target_position_id=position_id,
                first=before,second=after,
                observed_low_since_entry=low_since_entry,
                current_last_price=current_last,
                price_tick=tick,
                market_observed_at_ms=market_ms,
                now_ms=self.now_ms(),
            )
            if (
                type(reviewed) is not TrailingProposalReview
                or reviewed.status!="TRAILING_PROPOSAL_OBSERVED_NONAUTHORIZING"
                or reviewed.proposal is None
                or reviewed.proposal.proposed_stop!=projected.proposed_native_stop_price
                or reviewed.proposal.best_profit_pct!=projected.proposed_checkpoint.best_profit_pct
                or reviewed.proposal.armed is not projected.proposed_checkpoint.trail_armed
                or reviewed.account_bookends_equal is not True
                or reviewed.all_owned_full_volume_market_stops_observed is not True
                or reviewed.frozen_trailing_policy_matched is not True
                or reviewed.current_stop_above_last_price is not True
            ):
                raise V40RealStopPreflightHalted(
                    "V40_CURRENT_STOP_PROJECTION_OR_MARKET_DRIFT"
                )
            signal_id=await self._owned_signal_id(cp)
            # These are *now* supported by the independent immutable-bar
            # and signed same-account bookends above. Do not claim venue
            # stop execution atomicity or a completed stop amendment.
            witnessed=replace(
                reviewed,market_low_provenance_verified=True,
                signed_get_provenance_independently_verified=True,
            )
            return V40LiveStopReadback(
                checkpoints=checkpoints,target_signal_id=signal_id,
                preflight=witnessed,
            )
        except V40RealStopPreflightHalted:
            raise
        except BaseException:
            raise V40RealStopPreflightHalted(
                "V40_CURRENT_STOP_GET_SQL_CANDLE_OR_TERMINAL_UNVERIFIED"
            ) from None

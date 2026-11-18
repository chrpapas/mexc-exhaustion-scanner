"""Positive, independent GET-only native-close witness for owned V40 slots.

A candle touching SL75/trailing is not a real close. A slot is NOT free
until exact MEXC terminal stop order, linked market CLOSE_SHORT, complete
deals, exact position history and unchanged surviving native stops agree.

This reads a dedicated immutable V40 runtime ledger both before and after
the exchange history checks. It never writes accounting, releases a slot,
submits an order or creates an account permission. A separate audited
funding/settlement persistence port remains necessary.
"""
from __future__ import annotations

import re
from typing import Any

from app.t100_live_durable_recovery import (
    PositionCheckpoint,checkpoint_from_row,
)
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_snapshot_coordinator import _ledger_digest
from app.t100_live_owned_emergency_guardian import OwnedShort
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_v40_unreleased_verified_exit_handoff import (
    V40VerifiedExitProjection,
)
from app.t100_v40_unreleased_portfolio_native_close import (
    V40PortfolioCloseWitness,
    observe_one_native_exit_among_v40_survivors,
)


class V40PositiveCloseReadHalted(RuntimeError):
    """Finite no-secret reason; no provenance information is logged."""


class V40PositiveNativeCloseReadPort:
    def __init__(
        self, *, probe:ReadOnlyMexcProbe,
        ledger:PostgresFourSlotLedgerReader,
        independently_verified_account_scope_sha256:str,
    )->None:
        if (
            type(probe) is not ReadOnlyMexcProbe
            or type(ledger) is not PostgresFourSlotLedgerReader
            or type(independently_verified_account_scope_sha256) is not str
            or re.fullmatch(
                r"[a-f0-9]{64}",
                independently_verified_account_scope_sha256,
            ) is None
        ):
            raise V40PositiveCloseReadHalted(
                "V40_POSITIVE_CLOSE_SAME_ACCOUNT_PROBE_SCOPE_REQUIRED"
            )
        self.probe=probe
        self.ledger=ledger
        self.scope=independently_verified_account_scope_sha256

    async def observe(
        self,projection:V40VerifiedExitProjection,
    )->V40PortfolioCloseWitness:
        if (
            type(projection) is not V40VerifiedExitProjection
            or projection.status!=
                "V40_VERIFIED_OWNED_ONE_TO_FOUR_EXITS_PROJECTED_NONAUTHORIZING"
            or projection.verified_exchange_and_ledger_bookends is not True
            or projection.verified_durable_bar_cursors is not True
            or projection.live_execution_certified is not False
            or projection.exchange_posts!=0 or projection.database_writes!=0
            or projection.native_close_reconciliation_count!=1
            or projection.batch.native_close_reconciliation_count!=1
        ):
            raise V40PositiveCloseReadHalted(
                "V40_POSITIVE_CLOSE_ONE_CANDLE_CANDIDATE_REQUIRED"
            )
        candidates=[
            review for review in projection.batch.slots
            if review.requires_native_order_reconciliation is True
        ]
        if len(candidates)!=1 or candidates[0].requires_native_stop_amendment:
            raise V40PositiveCloseReadHalted(
                "V40_POSITIVE_CLOSE_CANDIDATE_NONUNIQUE"
            )
        candidate=candidates[0]
        try:
            first=await self.ledger.read_snapshot()
            ledger_hash=_ledger_digest(first)
            checkpoints=tuple(
                checkpoint_from_row(row) for row in first.checkpoints
            )
        except Exception:
            raise V40PositiveCloseReadHalted(
                "V40_POSITIVE_CLOSE_LEDGER_READ_OR_REVISION_FAILED"
            ) from None
        matching=[
            cp for cp in checkpoints
            if cp.position_id==candidate.position_id
            and cp.symbol==candidate.symbol
        ]
        if (
            len(matching)!=1
            or not 1<=len(checkpoints)<=4
            or len(checkpoints)!=projection.protected_position_count
            or len({cp.position_id for cp in checkpoints})!=len(checkpoints)
            or len({cp.symbol for cp in checkpoints})!=len(checkpoints)
            or any(cp.lifecycle_state!="OPEN" for cp in checkpoints)
            or candidate.requires_native_order_reconciliation is not True
            or candidate.decision.status !=
                "NATIVE_STOP_MAY_HAVE_TRIGGERED_RECONCILE_ONLY"
        ):
            raise V40PositiveCloseReadHalted(
                "V40_POSITIVE_CLOSE_CHECKPOINT_ORIGIN_DRIFT"
            )
        cp=matching[0]
        closing=OwnedShort(
            scope_sha256=self.scope,symbol=cp.symbol,
            position_id=cp.position_id,
            entry_external_oid=cp.external_oid,
            entry_order_id=cp.exchange_order_id,
            contracts=cp.contracts,stop_order_id=cp.stop_order_id,
            stop_price=cp.stop_price,
            checkpoint_version=cp.checkpoint_version,
        )
        survivors=tuple(
            other for other in checkpoints
            if other.position_id!=cp.position_id
        )
        try:
            witness=await observe_one_native_exit_among_v40_survivors(
                probe=self.probe,closing=closing,
                account_scope_sha256=self.scope,
                surviving=survivors,
            )
            second=await self.ledger.read_snapshot()
            if _ledger_digest(second)!=ledger_hash:
                raise V40PositiveCloseReadHalted(
                    "V40_POSITIVE_CLOSE_LEDGER_CHANGED_DURING_EXCHANGE_HISTORY"
                )
        except V40PositiveCloseReadHalted:
            raise
        except Exception:
            raise V40PositiveCloseReadHalted(
                "V40_POSITIVE_CLOSE_EXCHANGE_OR_LEDGER_READ_FAILED"
            ) from None
        if (
            type(witness) is not V40PortfolioCloseWitness
            or witness.closed_position_id not in (None,cp.position_id)
            or witness.slot_released is not False
            or witness.live_authorization is not False
            or witness.mexc_posts!=0 or witness.database_writes!=0
            or witness.surviving_protected_slots not in (
                0,len(survivors)
            )
            or (
                witness.status in (
                    "V40_PORTFOLIO_CLOSE_EXCHANGE_TP_LABEL_CONFLICT_UNRELEASED",
                    "V40_PORTFOLIO_CLOSE_EXCHANGE_SL_LABEL_OBSERVED_UNRELEASED",
                ) and (
                    witness.closed_position_id!=cp.position_id
                    or witness.surviving_protected_slots!=len(survivors)
                    or witness.exact_closed_position_history_verified is not True
                    or witness.portfolio_unchanged_across_bookends is not True
                    or witness.closing_fills is None
                    or witness.native_terminal is None
                )
            )
        ):
            raise V40PositiveCloseReadHalted(
                "V40_POSITIVE_CLOSE_WITNESS_IDENTITY_OR_COMPLETENESS_DRIFT"
            )
        return witness

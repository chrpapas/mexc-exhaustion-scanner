"""Only convert typed V40 postflight evidence into Discord business events.

NO MEXC network I/O, no trading permission, no DB writes, no outbox enqueue.
Caller must collect independently signed exchange and durable journal
evidence. Merely posting an entry/SL request is *never* sufficient.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal as D
from typing import Any

from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_native_stop_execution_audit import assess_native_short_market_stop_row
from app.t100_live_v40_protected_finalization import PreparedProtectedEntry
from app.t100_live_fill_ledger import VerifiedOrderFillSet
from app.t100_live_close_provenance import VerifiedStopCloseProvenance
from app.t100_live_position_accounting import VerifiedClosedPositionAccounting
from app.t100_v40_unreleased_trade_events import V40BusinessEvent, V40EventInvalid


def _utc(ts: datetime) -> str:
    if type(ts) is not datetime or ts.tzinfo is None or ts.utcoffset() is None:
        raise V40EventInvalid("V40_BUSINESS_EVENT_TIME_NOT_SIGNED_UTC")
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def verified_entry_and_native_sl_events(
    *,
    prepared: PreparedProtectedEntry,
    persisted_checkpoint: PositionCheckpoint,
    current_native_sl_get: dict[str, Any],
    post_commit_restart_recovered: bool,
    signed_get_account_complete: bool,
    observed_at: datetime,
) -> tuple[V40BusinessEvent, V40BusinessEvent]:
    """Requires actual committed entry/fills and a fresh native stop readback.

    The existing PreparedProtectedEntry is only an offline preparation; it
    must match the committed OPEN checkpoint and verified independent restart
    witness, otherwise **no** successful Discord notification is emitted.
    """
    if (
        type(prepared) is not PreparedProtectedEntry
        or type(persisted_checkpoint) is not PositionCheckpoint
        or type(current_native_sl_get) is not dict
        or post_commit_restart_recovered is not True
        or signed_get_account_complete is not True
        or type(prepared.fill_set) is not VerifiedOrderFillSet
    ):
        raise V40EventInvalid("V40_POST_COMMIT_EVENT_EVIDENCE_MISSING")
    saved = persisted_checkpoint
    original = prepared.checkpoint
    fill = prepared.fill_set
    if not (
        original == saved
        and saved.lifecycle_state == "OPEN"
        and saved.checkpoint_version >= 1
        and prepared.stage.signal_id > 0
        and prepared.intent.signal_id == prepared.stage.signal_id
        and saved.symbol == prepared.intent.symbol == fill.symbol
        and saved.external_oid == prepared.intent.external_oid
        and saved.position_id == prepared.position_id == fill.position_id
        and saved.exchange_order_id == prepared.order_id == fill.order_id
        and saved.contracts == fill.deal_volume
        and saved.entry_price == fill.average_price
        and saved.stop_order_id == str(current_native_sl_get.get("id"))
        and fill.side == "OPEN_SHORT"
        and fill.strategy_fully_filled_order_complete is True
        and fill.final_order_fill_history_complete is True
        and len(fill.fills) > 0
    ):
        raise V40EventInvalid("V40_POST_COMMIT_ENTRY_OR_FILL_DRIFT")
    sl = assess_native_short_market_stop_row(
        stop=current_native_sl_get,
        symbol=saved.symbol,
        position_id=saved.position_id,
        position_quantity=saved.contracts,
        maximum_short_stop_price=saved.stop_price,
        verified_filled_entry_order_id=saved.exchange_order_id,
    )
    if (
        sl.status != "NATIVE_MARKET_SHORT_SL_ROW_OBSERVED_NONAUTHORIZING"
        or sl.active_native_full_volume_market_sl_observed is not True
        or sl.stop_execution_certified is not False
    ):
        raise V40EventInvalid("V40_NATIVE_STOP_BUSINESS_PROOF_MISSING")
    time = _utc(observed_at)
    signal_id = prepared.stage.signal_id
    return (
        V40BusinessEvent(
            kind="ENTRY_FILLED",symbol=saved.symbol,signal_id=signal_id,
            position_id=saved.position_id,price=fill.average_price,
            evidence_status="MEXC_ENTRY_FILLS_VERIFIED",
            evidence_id="entry:"+fill.order_id,observed_at_utc=time,
        ),
        V40BusinessEvent(
            kind="NATIVE_SL_ACTIVE",symbol=saved.symbol,signal_id=signal_id,
            position_id=saved.position_id,price=saved.stop_price,
            evidence_status="MEXC_NATIVE_SL_GET_VERIFIED",
            evidence_id="stop:"+saved.stop_order_id,observed_at_utc=time,
        ),
    )


def verified_completed_stop_and_pnl_events(
    *,
    signal_id: int,
    position_id: str,
    symbol: str,
    close_provenance: VerifiedStopCloseProvenance,
    close_fills: VerifiedOrderFillSet,
    accounting: VerifiedClosedPositionAccounting,
    signed_account_flat_twice: bool,
    durable_checkpoint_closed: bool,
    observed_at: datetime,
) -> tuple[V40BusinessEvent, V40BusinessEvent]:
    """No SL-triggered or realized-PnL alert without exact close provenance."""
    if (
        type(close_provenance) is not VerifiedStopCloseProvenance
        or type(close_fills) is not VerifiedOrderFillSet
        or type(accounting) is not VerifiedClosedPositionAccounting
        or signed_account_flat_twice is not True
        or durable_checkpoint_closed is not True
    ):
        raise V40EventInvalid("V40_EXIT_ACCOUNT_OR_FILLS_NOT_FINAL")
    p = close_provenance
    f = close_fills
    r = accounting.reconciliation
    if not (
        p.position_id == f.position_id == r.position_id == position_id
        and p.symbol == f.symbol == symbol
        and p.close_order_id == f.order_id == accounting.close_order_id
        and p.stop_order_id == accounting.stop_order_id
        and p.trigger_side == "STOP_LOSS"
        and f.side == "CLOSE_SHORT"
        and f.strategy_fully_filled_order_complete
        and f.final_order_fill_history_complete
        and accounting.fill_history_complete
        and accounting.funding_history_complete
        and accounting.close_provenance_verified
        and r.status == "CLOSED_RECONCILED"
        and r.remaining_contracts == D("0")
        and r.realized_net_usdt.is_finite()
    ):
        raise V40EventInvalid("V40_EXIT_PROVENANCE_OR_ACCOUNTING_MISMATCH")
    time = _utc(observed_at)
    profit = r.realized_net_usdt > D("0")
    return (
        V40BusinessEvent(
            kind="SL_EXECUTED",symbol=symbol,signal_id=signal_id,
            position_id=position_id,
            evidence_id="stop:"+p.stop_order_id,
            evidence_status="MEXC_FINISHED_SL_AND_FILLS_VERIFIED",
            observed_at_utc=time,
        ),
        V40BusinessEvent(
            kind="EXIT_PROFIT" if profit else "EXIT_NONPROFIT",
            symbol=symbol,signal_id=signal_id,
            position_id=position_id,
            realized_pnl_usdt=r.realized_net_usdt,
            price=f.average_price,
            evidence_id="close:"+f.order_id,
            evidence_status="MEXC_CLOSED_POSITION_AND_PNL_VERIFIED",
            observed_at_utc=time,
        ),
    )

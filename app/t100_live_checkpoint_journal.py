"""Crash-safe durable T100 position checkpoint write protocol.

Pure validation plus an optional PostgreSQL persistence adapter. This module
does not call the exchange and never authorizes order/stop mutations.

Exchange state must already have been verified by the caller. The journal
then atomically updates the current checkpoint and appends an immutable
revision using compare-and-swap on checkpoint_version/lifecycle_state.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from app.t100_live_active_tpsl import effective_protective_quantity
from app.t100_live_durable_recovery import PositionCheckpoint


class CheckpointJournalHalted(RuntimeError):
    pass


ENTRY_PROTECTED = "ENTRY_PROTECTED"
STOP_VERIFIED = "STOP_VERIFIED"
REVIEW_LATCHED = "REVIEW_LATCHED"
CLOSING_LATCHED = "CLOSING_LATCHED"
CLOSE_VERIFIED = "CLOSE_VERIFIED"

WRITE_REASONS = frozenset({
    ENTRY_PROTECTED,
    STOP_VERIFIED,
    REVIEW_LATCHED,
    CLOSING_LATCHED,
    CLOSE_VERIFIED,
})


def _positive(raw: Any, label: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CheckpointJournalHalted(f"INVALID_{label}") from exc
    if not value.is_finite() or value <= 0:
        raise CheckpointJournalHalted(f"INVALID_{label}")
    return value


def _nonnegative(raw: Any, label: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CheckpointJournalHalted(f"INVALID_{label}") from exc
    if not value.is_finite() or value < 0:
        raise CheckpointJournalHalted(f"INVALID_{label}")
    return value


def _int(raw: Any, label: str) -> int:
    if isinstance(raw, bool):
        raise CheckpointJournalHalted(f"INVALID_{label}")
    try:
        value = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise CheckpointJournalHalted(f"INVALID_{label}") from exc
    if str(value) != str(raw):
        raise CheckpointJournalHalted(f"INVALID_{label}")
    return value


def _text(row: dict[str, Any], field: str, label: str | None = None) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise CheckpointJournalHalted(
            f"{label or field.upper()}_MISSING"
        )
    return value


def build_initial_checkpoint(
    *,
    intent: dict[str, Any],
    order: dict[str, Any],
    position: dict[str, Any],
    stop: dict[str, Any],
) -> PositionCheckpoint:
    """Create version-1 OPEN checkpoint from already-read exchange evidence."""
    if not all(isinstance(x, dict) for x in (intent, order, position, stop)):
        raise CheckpointJournalHalted("INITIAL_CHECKPOINT_EVIDENCE_MALFORMED")

    symbol = _text(intent, "symbol", "INTENT_SYMBOL")
    external_oid = _text(intent, "external_oid", "INTENT_EXTERNAL_OID")
    exchange_order_id = _text(
        intent, "exchange_order_id", "INTENT_EXCHANGE_ORDER_ID"
    )
    if intent.get("state") != "ACK":
        raise CheckpointJournalHalted("INTENT_NOT_ACK")

    contracts = _positive(intent.get("volume_contracts"), "INTENT_VOLUME")
    entry_stop = _positive(intent.get("stop_price"), "INTENT_STOP_PRICE")

    if str(order.get("externalOid") or "") != external_oid:
        raise CheckpointJournalHalted("ORDER_EXTERNAL_OID_DRIFT")
    if str(order.get("orderId") or "") != exchange_order_id:
        raise CheckpointJournalHalted("ORDER_ID_DRIFT")
    if str(order.get("symbol") or "") != symbol:
        raise CheckpointJournalHalted("ORDER_SYMBOL_DRIFT")
    if _int(order.get("side"), "ORDER_SIDE") != 3:
        raise CheckpointJournalHalted("ORDER_SIDE_DRIFT")
    if _int(order.get("state"), "ORDER_STATE") != 3:
        raise CheckpointJournalHalted("ORDER_NOT_FINAL_FILLED")
    if _positive(order.get("dealVol"), "ORDER_DEAL_VOLUME") != contracts:
        raise CheckpointJournalHalted("ORDER_VOLUME_DRIFT")

    position_id = str(order.get("positionId") or "")
    if not position_id:
        raise CheckpointJournalHalted("ORDER_POSITION_ID_MISSING")
    entry_price = _positive(order.get("dealAvgPrice"), "ORDER_AVERAGE_PRICE")

    if str(position.get("positionId") or position.get("id") or "") != position_id:
        raise CheckpointJournalHalted("POSITION_ID_DRIFT")
    if str(position.get("symbol") or "") != symbol:
        raise CheckpointJournalHalted("POSITION_SYMBOL_DRIFT")
    if _int(position.get("positionType"), "POSITION_SIDE") != 2:
        raise CheckpointJournalHalted("POSITION_SIDE_DRIFT")
    if _int(position.get("state"), "POSITION_STATE") != 1:
        raise CheckpointJournalHalted("POSITION_NOT_HOLDING")
    if _positive(position.get("holdVol"), "POSITION_VOLUME") != contracts:
        raise CheckpointJournalHalted("POSITION_VOLUME_DRIFT")
    hold_avg = _positive(position.get("holdAvgPrice"), "POSITION_ENTRY_PRICE")
    tolerance = max(Decimal("0.00000001"), entry_price * Decimal("0.00000001"))
    if abs(hold_avg - entry_price) > tolerance:
        raise CheckpointJournalHalted("POSITION_ENTRY_PRICE_DRIFT")

    stop_order_id = str(stop.get("id") or stop.get("stopPlanOrderId") or "")
    if not stop_order_id:
        raise CheckpointJournalHalted("STOP_ID_MISSING")
    if str(stop.get("positionId") or "") != position_id:
        raise CheckpointJournalHalted("STOP_POSITION_ID_DRIFT")
    if stop.get("symbol") not in (None, symbol):
        raise CheckpointJournalHalted("STOP_SYMBOL_DRIFT")
    if _int(stop.get("state"), "STOP_STATE") != 1:
        raise CheckpointJournalHalted("STOP_NOT_ACTIVE")
    stop_price = _positive(stop.get("stopLossPrice"), "STOP_PRICE")
    if stop_price > entry_stop:
        raise CheckpointJournalHalted("INITIAL_STOP_LOOSER_THAN_INTENT")
    effective_qty = effective_protective_quantity(
        stop,
        position_quantity=contracts,
    )
    if effective_qty is None or effective_qty != contracts:
        raise CheckpointJournalHalted("STOP_VOLUME_DRIFT")

    return PositionCheckpoint(
        symbol=symbol,
        external_oid=external_oid,
        exchange_order_id=exchange_order_id,
        position_id=position_id,
        contracts=contracts,
        entry_price=entry_price,
        stop_order_id=stop_order_id,
        stop_price=stop_price,
        best_profit_pct=Decimal("0"),
        armed=False,
        lifecycle_state="OPEN",
        checkpoint_version=1,
    )


def build_verified_stop_checkpoint(
    previous: PositionCheckpoint,
    *,
    amendment: dict[str, Any],
    stop: dict[str, Any],
    best_profit_pct: Any,
    armed: bool,
) -> PositionCheckpoint:
    """Advance checkpoint only after stop amendment is independently VERIFIED."""
    if not isinstance(previous, PositionCheckpoint):
        raise CheckpointJournalHalted("PREVIOUS_CHECKPOINT_INVALID")
    if previous.lifecycle_state != "OPEN":
        raise CheckpointJournalHalted("STOP_UPDATE_REQUIRES_OPEN_CHECKPOINT")
    if not isinstance(amendment, dict) or not isinstance(stop, dict):
        raise CheckpointJournalHalted("STOP_UPDATE_EVIDENCE_MALFORMED")
    if amendment.get("state") != "VERIFIED":
        raise CheckpointJournalHalted("STOP_AMENDMENT_NOT_VERIFIED")
    if str(amendment.get("symbol") or "") != previous.symbol:
        raise CheckpointJournalHalted("AMENDMENT_SYMBOL_DRIFT")
    if str(amendment.get("position_id") or "") != previous.position_id:
        raise CheckpointJournalHalted("AMENDMENT_POSITION_ID_DRIFT")
    if str(amendment.get("stop_order_id") or "") != previous.stop_order_id:
        raise CheckpointJournalHalted("AMENDMENT_STOP_ID_DRIFT")

    previous_stop = _positive(
        amendment.get("previous_stop"), "AMENDMENT_PREVIOUS_STOP"
    )
    proposed_stop = _positive(
        amendment.get("proposed_stop"), "AMENDMENT_PROPOSED_STOP"
    )
    if previous_stop != previous.stop_price:
        raise CheckpointJournalHalted("AMENDMENT_PREVIOUS_STOP_DRIFT")
    if proposed_stop >= previous.stop_price:
        raise CheckpointJournalHalted("AMENDMENT_DOES_NOT_TIGHTEN")

    if str(stop.get("positionId") or "") != previous.position_id:
        raise CheckpointJournalHalted("STOP_POSITION_ID_DRIFT")
    current_stop_id = str(stop.get("id") or stop.get("stopPlanOrderId") or "")
    if current_stop_id != previous.stop_order_id:
        raise CheckpointJournalHalted("STOP_ID_DRIFT")
    if stop.get("symbol") not in (None, previous.symbol):
        raise CheckpointJournalHalted("STOP_SYMBOL_DRIFT")
    if _int(stop.get("state"), "STOP_STATE") != 1:
        raise CheckpointJournalHalted("STOP_NOT_ACTIVE")
    actual_stop = _positive(stop.get("stopLossPrice"), "STOP_PRICE")
    if actual_stop > proposed_stop or actual_stop >= previous.stop_price:
        raise CheckpointJournalHalted("STOP_NOT_VERIFIED_TIGHTER")
    effective_qty = effective_protective_quantity(
        stop,
        position_quantity=previous.contracts,
    )
    if effective_qty is None or effective_qty != previous.contracts:
        raise CheckpointJournalHalted("STOP_VOLUME_DRIFT")

    best = _nonnegative(best_profit_pct, "BEST_PROFIT_PCT")
    if type(armed) is not bool:
        raise CheckpointJournalHalted("INVALID_ARMED")
    if best < previous.best_profit_pct:
        raise CheckpointJournalHalted("BEST_PROFIT_REGRESSION")
    if previous.armed and not armed:
        raise CheckpointJournalHalted("ARMED_STATE_CANNOT_DISARM")
    if armed != (best >= Decimal("14")):
        raise CheckpointJournalHalted("TRAILING_STATE_INCOHERENT")

    return replace(
        previous,
        stop_price=actual_stop,
        best_profit_pct=best,
        armed=armed,
        checkpoint_version=previous.checkpoint_version + 1,
    )


def build_lifecycle_checkpoint(
    previous: PositionCheckpoint,
    *,
    target: str,
    position_absent: bool = False,
    stop_absent: bool = False,
    close_provenance_verified: bool = False,
) -> PositionCheckpoint:
    """Build fail-closed lifecycle transitions; no exchange mutation occurs."""
    if not isinstance(previous, PositionCheckpoint):
        raise CheckpointJournalHalted("PREVIOUS_CHECKPOINT_INVALID")

    if target == "REVIEW":
        if previous.lifecycle_state != "OPEN":
            raise CheckpointJournalHalted("REVIEW_REQUIRES_OPEN")
    elif target == "CLOSING":
        if previous.lifecycle_state not in {"OPEN", "REVIEW"}:
            raise CheckpointJournalHalted("CLOSING_REQUIRES_OPEN_OR_REVIEW")
    elif target == "CLOSED":
        if previous.lifecycle_state != "CLOSING":
            raise CheckpointJournalHalted("CLOSED_REQUIRES_CLOSING")
        if (
            position_absent is not True
            or stop_absent is not True
            or close_provenance_verified is not True
        ):
            raise CheckpointJournalHalted("CLOSE_EVIDENCE_INCOMPLETE")
    else:
        raise CheckpointJournalHalted("INVALID_LIFECYCLE_TARGET")

    return replace(
        previous,
        lifecycle_state=target,
        checkpoint_version=previous.checkpoint_version + 1,
    )


def _validate_checkpoint_shape(cp: PositionCheckpoint) -> None:
    if not isinstance(cp, PositionCheckpoint):
        raise CheckpointJournalHalted("CHECKPOINT_INVALID")
    for field in (
        "symbol", "external_oid", "exchange_order_id",
        "position_id", "stop_order_id",
    ):
        value = getattr(cp, field)
        if not isinstance(value, str) or not value:
            raise CheckpointJournalHalted(
                f"CHECKPOINT_{field.upper()}_MISSING"
            )
    for field in ("contracts", "entry_price", "stop_price"):
        value = getattr(cp, field)
        if (
            not isinstance(value, Decimal)
            or not value.is_finite()
            or value <= 0
        ):
            raise CheckpointJournalHalted(
                f"CHECKPOINT_{field.upper()}_INVALID"
            )
    if (
        not isinstance(cp.best_profit_pct, Decimal)
        or not cp.best_profit_pct.is_finite()
        or cp.best_profit_pct < 0
    ):
        raise CheckpointJournalHalted(
            "CHECKPOINT_BEST_PROFIT_PCT_INVALID"
        )
    if type(cp.armed) is not bool:
        raise CheckpointJournalHalted("CHECKPOINT_ARMED_INVALID")
    if cp.armed != (cp.best_profit_pct >= Decimal("14")):
        raise CheckpointJournalHalted(
            "CHECKPOINT_TRAILING_STATE_INCOHERENT"
        )
    if cp.lifecycle_state not in {"OPEN", "REVIEW", "CLOSING", "CLOSED"}:
        raise CheckpointJournalHalted(
            "CHECKPOINT_LIFECYCLE_STATE_INVALID"
        )
    if (
        type(cp.checkpoint_version) is not int
        or cp.checkpoint_version <= 0
    ):
        raise CheckpointJournalHalted(
            "CHECKPOINT_VERSION_INVALID"
        )


def validate_checkpoint_transition(
    previous: PositionCheckpoint | None,
    current: PositionCheckpoint,
    *,
    reason: str,
) -> None:
    if reason not in WRITE_REASONS:
        raise CheckpointJournalHalted("INVALID_CHECKPOINT_WRITE_REASON")
    _validate_checkpoint_shape(current)

    if previous is None:
        if reason != ENTRY_PROTECTED:
            raise CheckpointJournalHalted("INITIAL_REASON_MUST_BE_ENTRY_PROTECTED")
        if current.checkpoint_version != 1 or current.lifecycle_state != "OPEN":
            raise CheckpointJournalHalted("INITIAL_CHECKPOINT_STATE_INVALID")
        return

    _validate_checkpoint_shape(previous)
    immutable = (
        "symbol", "external_oid", "exchange_order_id", "position_id",
        "contracts", "entry_price", "stop_order_id",
    )
    for field in immutable:
        if getattr(previous, field) != getattr(current, field):
            raise CheckpointJournalHalted(
                f"IMMUTABLE_{field.upper()}_DRIFT"
            )
    if current.checkpoint_version != previous.checkpoint_version + 1:
        raise CheckpointJournalHalted("CHECKPOINT_VERSION_NOT_SEQUENTIAL")
    if current.stop_price > previous.stop_price:
        raise CheckpointJournalHalted("CHECKPOINT_STOP_LOOSENED")
    if current.best_profit_pct < previous.best_profit_pct:
        raise CheckpointJournalHalted("CHECKPOINT_BEST_PROFIT_REGRESSION")
    if previous.armed and not current.armed:
        raise CheckpointJournalHalted("CHECKPOINT_ARMED_STATE_REGRESSION")

    expected: tuple[str, str, str]
    if reason == STOP_VERIFIED:
        expected = ("OPEN", "OPEN", STOP_VERIFIED)
        if current.stop_price >= previous.stop_price:
            raise CheckpointJournalHalted("STOP_VERIFIED_MUST_TIGHTEN")
    elif reason == REVIEW_LATCHED:
        expected = ("OPEN", "REVIEW", REVIEW_LATCHED)
        if (
            current.stop_price != previous.stop_price
            or current.best_profit_pct != previous.best_profit_pct
            or current.armed != previous.armed
        ):
            raise CheckpointJournalHalted("REVIEW_MUST_NOT_MUTATE_POSITION_STATE")
    elif reason == CLOSING_LATCHED:
        if previous.lifecycle_state not in {"OPEN", "REVIEW"}:
            raise CheckpointJournalHalted("CLOSING_SOURCE_INVALID")
        expected = (previous.lifecycle_state, "CLOSING", CLOSING_LATCHED)
        if (
            current.stop_price != previous.stop_price
            or current.best_profit_pct != previous.best_profit_pct
            or current.armed != previous.armed
        ):
            raise CheckpointJournalHalted("CLOSING_MUST_NOT_MUTATE_POSITION_STATE")
    elif reason == CLOSE_VERIFIED:
        expected = ("CLOSING", "CLOSED", CLOSE_VERIFIED)
        if (
            current.stop_price != previous.stop_price
            or current.best_profit_pct != previous.best_profit_pct
            or current.armed != previous.armed
        ):
            raise CheckpointJournalHalted("CLOSE_MUST_NOT_MUTATE_POSITION_STATE")
    else:
        raise CheckpointJournalHalted("ENTRY_REASON_ONLY_VALID_FOR_INITIAL")

    if (previous.lifecycle_state, current.lifecycle_state, reason) != expected:
        raise CheckpointJournalHalted("CHECKPOINT_LIFECYCLE_REASON_MISMATCH")


class PostgresCheckpointJournal:
    """Atomic current-row + append-only-revision persistence adapter."""

    def __init__(self, pool: Any):
        self.pool = pool

    @staticmethod
    def _args(cp: PositionCheckpoint) -> tuple[Any, ...]:
        return (
            cp.position_id,
            cp.symbol,
            cp.external_oid,
            cp.exchange_order_id,
            cp.contracts,
            cp.entry_price,
            cp.stop_order_id,
            cp.stop_price,
            cp.best_profit_pct,
            cp.armed,
            cp.lifecycle_state,
            cp.checkpoint_version,
        )

    async def create_initial(self, current: PositionCheckpoint) -> None:
        validate_checkpoint_transition(None, current, reason=ENTRY_PROTECTED)
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchval(
                        """INSERT INTO t100_live_position_checkpoints
                           (position_id,symbol,external_oid,exchange_order_id,
                            contracts,entry_price,stop_order_id,stop_price,
                            best_profit_pct,armed,lifecycle_state,
                            checkpoint_version,last_write_reason)
                           VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                           ON CONFLICT DO NOTHING RETURNING position_id""",
                        *self._args(current), ENTRY_PROTECTED,
                    )
                    if row is None:
                        raise CheckpointJournalHalted(
                            "INITIAL_CHECKPOINT_CONFLICT"
                        )
                    await self._append_revision(
                        conn, current, ENTRY_PROTECTED
                    )
        except CheckpointJournalHalted:
            raise
        except Exception as exc:
            raise CheckpointJournalHalted(
                "INITIAL_CHECKPOINT_TRANSACTION_FAILED"
            ) from exc

    async def advance(
        self,
        previous: PositionCheckpoint,
        current: PositionCheckpoint,
        *,
        reason: str,
    ) -> None:
        validate_checkpoint_transition(previous, current, reason=reason)
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchval(
                        """UPDATE t100_live_position_checkpoints
                           SET stop_order_id=$7,stop_price=$8,
                               best_profit_pct=$9,armed=$10,
                               lifecycle_state=$11,checkpoint_version=$12,
                               last_write_reason=$13,
                               updated_at=now(),verified_at=now()
                           WHERE position_id=$1
                             AND symbol=$2
                             AND external_oid=$3
                             AND exchange_order_id=$4
                             AND contracts=$5
                             AND entry_price=$6
                             AND checkpoint_version=$14
                             AND lifecycle_state=$15
                           RETURNING position_id""",
                        *self._args(current), reason,
                        previous.checkpoint_version,
                        previous.lifecycle_state,
                    )
                    if row is None:
                        raise CheckpointJournalHalted(
                            "CHECKPOINT_CAS_FAILURE"
                        )
                    if reason == CLOSE_VERIFIED:
                        intent_row = await conn.fetchval(
                            """UPDATE t100_live_order_intents
                               SET state='CLOSED',updated_at=now()
                               WHERE external_oid=$1
                                 AND exchange_order_id=$2
                                 AND state='ACK'
                               RETURNING id""",
                            current.external_oid,
                            current.exchange_order_id,
                        )
                        if intent_row is None:
                            raise CheckpointJournalHalted(
                                "CLOSE_INTENT_CAS_FAILURE"
                            )
                    await self._append_revision(conn, current, reason)
        except CheckpointJournalHalted:
            raise
        except Exception as exc:
            raise CheckpointJournalHalted(
                "CHECKPOINT_TRANSACTION_FAILED"
            ) from exc

    async def _append_revision(
        self,
        conn: Any,
        cp: PositionCheckpoint,
        reason: str,
    ) -> None:
        row = await conn.fetchval(
            """INSERT INTO t100_live_position_checkpoint_revisions
               (position_id,checkpoint_version,write_reason,symbol,
                external_oid,exchange_order_id,contracts,entry_price,
                stop_order_id,stop_price,best_profit_pct,armed,
                lifecycle_state)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
               RETURNING revision_id""",
            cp.position_id,
            cp.checkpoint_version,
            reason,
            cp.symbol,
            cp.external_oid,
            cp.exchange_order_id,
            cp.contracts,
            cp.entry_price,
            cp.stop_order_id,
            cp.stop_price,
            cp.best_profit_pct,
            cp.armed,
            cp.lifecycle_state,
        )
        if row is None:
            raise CheckpointJournalHalted("CHECKPOINT_REVISION_INSERT_FAILED")

"""Read-only, fail-closed T100/V40 trailing proposal gate for 1-4 owned shorts.

Composes frozen 14% activation / 0.5pp trailing math with stable full-account
inventory counts, durable checkpoints and strict position-origin MARKET SL
readback. Does NOT submit orders, tighten stops, write journals/DBs, assume
market-low provenance, certify exchange atomicity or authorize live trading.

The caller must still independently establish signed-GET provenance of BOTH
snapshots and causal provenance of the lowest price since entry before any
separately gated mutation attempt. Fabricating inputs cannot grant a permit.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_four_slot_snapshot_coordinator import (
    _valid_counts, _venue_digest,
)
from app.t100_live_native_stop_execution_audit import (
    assess_native_short_market_stop_row,
)
from app.t100_live_supervisor import StopProposal
from app.t100_live_trailing import next_short_stop


@dataclass(frozen=True)
class TrailingBookend:
    positions: list[dict[str, Any]]
    stops: list[dict[str, Any]]
    completeness: dict[str, Any]
    observed_at_ms: int


@dataclass(frozen=True)
class TrailingProposalReview:
    status: str
    proposal: StopProposal | None = None
    account_bookends_equal: bool = False
    all_owned_full_volume_market_stops_observed: bool = False
    frozen_trailing_policy_matched: bool = False
    current_stop_above_last_price: bool = False
    market_low_provenance_verified: bool = False
    signed_get_provenance_independently_verified: bool = False
    exchange_atomicity_verified: bool = False
    native_stop_execution_certified: bool = False
    mutation_attempted: bool = False
    live_stop_mutation_allowed: bool = False
    live_authorization: bool = False
    read_only: bool = True


def _positive_decimal(value: Any) -> Decimal | None:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        return None
    return value


def _nonnegative_decimal(value: Any) -> Decimal | None:
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        return None
    return value


def _id(value: Any) -> str | None:
    if type(value) not in (int, str):
        return None
    v = str(value)
    return v if v.isascii() and v.isdecimal() and v[0] != "0" else None


def review_t100_trailing_candidate(
    *,
    checkpoints: list[PositionCheckpoint],
    target_position_id: str,
    first: TrailingBookend,
    second: TrailingBookend,
    observed_low_since_entry: Decimal,
    current_last_price: Decimal,
    price_tick: Decimal,
    market_observed_at_ms: int,
    now_ms: int,
) -> TrailingProposalReview:
    def blocked(reason: str, **kwargs: Any) -> TrailingProposalReview:
        return TrailingProposalReview(status=reason, **kwargs)

    if (
        type(checkpoints) is not list
        or not 1 <= len(checkpoints) <= 4
        or type(first) is not TrailingBookend
        or type(second) is not TrailingBookend
        or type(target_position_id) is not str
        or _id(target_position_id) != target_position_id
        or _positive_decimal(observed_low_since_entry) is None
        or _positive_decimal(current_last_price) is None
        or _positive_decimal(price_tick) is None
        or type(now_ms) is not int or now_ms <= 0
        or type(market_observed_at_ms) is not int
        or any(
            type(book.observed_at_ms) is not int or
            book.observed_at_ms <= 0
            for book in (first, second)
        )
    ):
        return blocked("TRAILING_INPUT_OR_TIMESTAMP_INVALID")

    # A stale or future-dated input must not yield an executable proposal.
    if (
        first.observed_at_ms > second.observed_at_ms
        or second.observed_at_ms > now_ms + 2000
        or market_observed_at_ms > now_ms + 2000
        or first.observed_at_ms < now_ms - 10000
        or second.observed_at_ms < now_ms - 10000
        or market_observed_at_ms < now_ms - 10000
    ):
        return blocked("TRAILING_BOOKEND_OR_MARKET_STALE")

    if (
        any(type(c) is not PositionCheckpoint for c in checkpoints)
        or any(
            c.lifecycle_state != "OPEN"
            or type(c.checkpoint_version) is not int
            or c.checkpoint_version < 1
            or _id(c.position_id) != c.position_id
            or _id(c.stop_order_id) != c.stop_order_id
            or not c.symbol.endswith("_USDT")
            or not c.external_oid
            or _positive_decimal(c.contracts) is None
            or _positive_decimal(c.entry_price) is None
            or _positive_decimal(c.stop_price) is None
            or _nonnegative_decimal(c.best_profit_pct) is None
            or type(c.armed) is not bool
            or c.armed != (c.best_profit_pct >= Decimal("14"))
            for c in checkpoints
        )
    ):
        return blocked("TRAILING_DURABLE_CHECKPOINT_INVALID")

    positions_ids = [c.position_id for c in checkpoints]
    if (
        len(set(positions_ids)) != len(checkpoints)
        or len({c.symbol for c in checkpoints}) != len(checkpoints)
        or target_position_id not in positions_ids
    ):
        return blocked("TRAILING_OWNERSHIP_OR_SLOT_AMBIGUOUS")

    try:
        for book in (first, second):
            if (
                type(book.positions) is not list
                or type(book.stops) is not list
                or any(type(row) is not dict for row in book.positions + book.stops)
                or not _valid_counts(
                    book.completeness, len(book.positions), len(book.stops),
                )
            ):
                return blocked("TRAILING_ACCOUNT_COMPLETENESS_UNVERIFIED")
        if (
            _venue_digest(first.positions, first.stops)
            != _venue_digest(second.positions, second.stops)
        ):
            return blocked("TRAILING_CRITICAL_ACCOUNT_DRIFT")
    except (ValueError, TypeError, KeyError, OverflowError):
        return blocked("TRAILING_CRITICAL_ACCOUNT_DIGEST_UNVERIFIED")

    equal = {"account_bookends_equal": True}
    # No adoption of operator-owned positions or stops in this strict path.
    if (
        len(second.positions) != len(checkpoints)
        or len(second.stops) != len(checkpoints)
    ):
        return blocked("TRAILING_UNOWNED_INVENTORY_PRESENT", **equal)
    position_rows: dict[str, dict[str, Any]] = {}
    stop_rows: dict[str, dict[str, Any]] = {}
    for row in second.positions:
        pid = _id(row.get("positionId"))
        if pid is None or pid in position_rows:
            return blocked("TRAILING_POSITION_IDENTITY_UNVERIFIED", **equal)
        position_rows[pid] = row
    for row in second.stops:
        pid = _id(row.get("positionId"))
        if pid is None or pid in stop_rows:
            return blocked("TRAILING_STOP_IDENTITY_UNVERIFIED", **equal)
        stop_rows[pid] = row
    if set(position_rows) != set(positions_ids) or set(stop_rows) != set(positions_ids):
        return blocked("TRAILING_POSITION_STOP_OWNERSHIP_MISMATCH", **equal)

    for checkpoint in checkpoints:
        position = position_rows[checkpoint.position_id]
        stop = stop_rows[checkpoint.position_id]
        try:
            volume = Decimal(str(position.get("holdVol")))
            leverage = int(position.get("leverage"))
        except (TypeError, ValueError, ArithmeticError):
            return blocked("TRAILING_POSITION_FIELDS_INVALID", **equal)
        if (
            not volume.is_finite() or volume != checkpoint.contracts
            or position.get("symbol") != checkpoint.symbol
            or type(position.get("positionType")) is not int
            or position["positionType"] != 2
            or type(position.get("state")) is not int
            or position["state"] != 1
            or type(position.get("openType")) is not int
            or position["openType"] not in (1, 2)
            or type(position.get("leverage")) is not int
            or leverage <= 0
            or _id(stop.get("id")) != checkpoint.stop_order_id
        ):
            return blocked("TRAILING_CHECKPOINT_POSITION_DRIFT", **equal)
        audit = assess_native_short_market_stop_row(
            stop=stop,
            symbol=checkpoint.symbol,
            position_id=checkpoint.position_id,
            position_quantity=checkpoint.contracts,
            maximum_short_stop_price=checkpoint.stop_price,
        )
        if not audit.active_native_full_volume_market_sl_observed:
            return blocked(
                "TRAILING_NATIVE_STOP_UNVERIFIED:" + audit.status,
                **equal,
            )

    evidence = dict(
        account_bookends_equal=True,
        all_owned_full_volume_market_stops_observed=True,
    )
    cp = next(c for c in checkpoints if c.position_id == target_position_id)
    # The caller must supply the causal low since entry (not merely a
    # recent low). This gate CANNOT certify low provenance by itself.
    if observed_low_since_entry > current_last_price:
        return blocked("TRAILING_LOW_INCONSISTENT_WITH_MARKET", **evidence)
    try:
        step = next_short_stop(
            entry=cp.entry_price,
            low=observed_low_since_entry,
            current_stop=cp.stop_price,
            best_profit_pct=cp.best_profit_pct,
            price_tick=price_tick,
            armed=cp.armed,
        )
    except (ValueError, TypeError, ArithmeticError):
        return blocked("TRAILING_FROZEN_POLICY_INPUT_INVALID", **evidence)

    if not step.active or not step.changed:
        return blocked("TRAILING_NO_STOP_UPDATE_DUE", **evidence)

    # For a SHORT, tightening below the current price can cause immediate
    # trigger. A limit tick above the current market is a minimum sanity
    # check, not a guarantee about exchange trigger or fair price.
    if step.stop_price <= current_last_price + price_tick:
        return blocked("TRAILING_PROPOSED_STOP_ALREADY_CROSSED", **evidence)

    proposal = StopProposal(
        symbol=cp.symbol,
        position_id=cp.position_id,
        stop_order_id=cp.stop_order_id,
        old_stop=cp.stop_price,
        proposed_stop=step.stop_price,
        best_profit_pct=step.best_profit_pct,
        armed=step.active,
    )
    return TrailingProposalReview(
        status="TRAILING_PROPOSAL_OBSERVED_NONAUTHORIZING",
        proposal=proposal,
        account_bookends_equal=True,
        all_owned_full_volume_market_stops_observed=True,
        frozen_trailing_policy_matched=True,
        current_stop_above_last_price=True,
    )

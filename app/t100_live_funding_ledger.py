"""Immutable final funding ledger for a closed T100 position.

Funding history is position-scoped and metadata-paginated by MEXC. To avoid
declaring funding final while a late settlement row is still appearing, the
closed position history must be collected twice from independently complete
page scans with identical canonical rows and totalCount.

No exchange mutation occurs here.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any

from app.t100_live_funding_probe import assess_funding_economics
from app.t100_live_pagination import CompletePages


class VerifiedFundingLedgerHalted(RuntimeError):
    pass


def _decimal(raw: Any) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise VerifiedFundingLedgerHalted("INVALID_FUNDING_NUMERIC") from exc
    if not value.is_finite():
        raise VerifiedFundingLedgerHalted("INVALID_FUNDING_NUMERIC")
    return value


def _integer(raw: Any) -> int:
    if isinstance(raw, bool):
        raise VerifiedFundingLedgerHalted("INVALID_FUNDING_INTEGER")
    try:
        value = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise VerifiedFundingLedgerHalted("INVALID_FUNDING_INTEGER") from exc
    if str(value) != str(raw):
        raise VerifiedFundingLedgerHalted("INVALID_FUNDING_INTEGER")
    return value


@dataclass(frozen=True)
class VerifiedFundingEvent:
    event_id: str
    position_id: str
    symbol: str
    position_type: int
    position_value: Decimal
    funding_cashflow_usdt: Decimal
    rate: Decimal
    settle_time_ms: int


@dataclass(frozen=True)
class VerifiedPositionFundingSet:
    position_id: str
    events: tuple[VerifiedFundingEvent, ...]
    total_count: int
    funding_digest_sha256: str
    economics_verified: bool
    double_collection_finality_verified: bool = True


def _canonical_rows(rows: tuple[dict[str, Any], ...]) -> tuple[str, ...]:
    safe = []
    for row in rows:
        if not isinstance(row, dict):
            raise VerifiedFundingLedgerHalted("FUNDING_ROW_INVALID")
        safe.append(json.dumps(
            {
                "id": row.get("id"),
                "symbol": row.get("symbol"),
                "positionType": row.get("positionType"),
                "positionValue": row.get("positionValue"),
                "funding": row.get("funding"),
                "rate": row.get("rate"),
                "settleTime": row.get("settleTime"),
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ))
    return tuple(sorted(safe))


def build_verified_closed_position_funding_set(
    *,
    position_id: str,
    first: CompletePages,
    second: CompletePages,
) -> VerifiedPositionFundingSet:
    if not isinstance(position_id, str) or not position_id:
        raise VerifiedFundingLedgerHalted("FUNDING_POSITION_ID_MISSING")
    if not isinstance(first, CompletePages) or not isinstance(second, CompletePages):
        raise VerifiedFundingLedgerHalted("FUNDING_PAGE_SET_INVALID")
    if not first.complete or not second.complete:
        raise VerifiedFundingLedgerHalted("FUNDING_PAGINATION_INCOMPLETE")
    if first.total != second.total:
        raise VerifiedFundingLedgerHalted("FUNDING_TOTAL_CHANGED")
    if len(first.rows) != first.total or len(second.rows) != second.total:
        raise VerifiedFundingLedgerHalted("FUNDING_TOTAL_ROW_COUNT_MISMATCH")

    try:
        canonical_first = _canonical_rows(first.rows)
        canonical_second = _canonical_rows(second.rows)
    except (TypeError, ValueError, OverflowError) as exc:
        raise VerifiedFundingLedgerHalted(
            "FUNDING_CANONICALIZATION_FAILED"
        ) from exc
    if canonical_first != canonical_second:
        raise VerifiedFundingLedgerHalted(
            "FUNDING_ROWS_CHANGED_BETWEEN_COMPLETE_COLLECTIONS"
        )

    rows = list(second.rows)
    economics = assess_funding_economics(rows)
    economics_verified = (
        not rows
        or (
            economics.get("funding_cashflow_sign_semantics_observed") is True
            and economics.get("funding_formula_semantics_observed") is True
        )
    )
    if not economics_verified:
        raise VerifiedFundingLedgerHalted(
            "FUNDING_ECONOMICS_UNVERIFIED"
        )

    events: list[VerifiedFundingEvent] = []
    seen: set[str] = set()
    for row in rows:
        event_id = str(row.get("id") or "")
        symbol = row.get("symbol")
        side = _integer(row.get("positionType"))
        position_value = _decimal(row.get("positionValue"))
        funding = _decimal(row.get("funding"))
        rate = _decimal(row.get("rate"))
        settle_time = _integer(row.get("settleTime"))
        if (
            not event_id
            or event_id in seen
            or not isinstance(symbol, str)
            or not symbol
            or side not in {1, 2}
            or position_value < 0
            or settle_time <= 0
        ):
            raise VerifiedFundingLedgerHalted(
                "FUNDING_EVENT_CONTRACT_INVALID"
            )
        seen.add(event_id)
        events.append(VerifiedFundingEvent(
            event_id=event_id,
            position_id=position_id,
            symbol=symbol,
            position_type=side,
            position_value=position_value,
            funding_cashflow_usdt=funding,
            rate=rate,
            settle_time_ms=settle_time,
        ))

    ordered = tuple(sorted(
        events,
        key=lambda x: (x.settle_time_ms, x.event_id),
    ))
    payload = [
        {
            "event_id": x.event_id,
            "position_id": x.position_id,
            "symbol": x.symbol,
            "position_type": x.position_type,
            "position_value": str(x.position_value),
            "funding_cashflow_usdt": str(x.funding_cashflow_usdt),
            "rate": str(x.rate),
            "settle_time_ms": x.settle_time_ms,
        }
        for x in ordered
    ]
    digest = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()

    return VerifiedPositionFundingSet(
        position_id=position_id,
        events=ordered,
        total_count=second.total,
        funding_digest_sha256=digest,
        economics_verified=economics_verified,
    )


class PostgresVerifiedFundingLedger:
    """Append-only immutable final funding set persistence."""

    def __init__(self, pool: Any):
        self.pool = pool

    async def persist(self, funding_set: VerifiedPositionFundingSet) -> str:
        if not isinstance(funding_set, VerifiedPositionFundingSet):
            raise VerifiedFundingLedgerHalted("VERIFIED_FUNDING_SET_INVALID")
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    existing = await conn.fetchrow(
                        """SELECT funding_digest_sha256,total_count
                           FROM t100_live_position_funding_sets
                           WHERE position_id=$1""",
                        funding_set.position_id,
                    )
                    if existing is not None:
                        if (
                            str(existing["funding_digest_sha256"])
                            != funding_set.funding_digest_sha256
                            or int(existing["total_count"])
                            != funding_set.total_count
                        ):
                            raise VerifiedFundingLedgerHalted(
                                "VERIFIED_FUNDING_SET_IMMUTABILITY_MISMATCH"
                            )
                        return "ALREADY_VERIFIED"

                    inserted = await conn.fetchval(
                        """INSERT INTO t100_live_position_funding_sets
                           (position_id,total_count,funding_digest_sha256,
                            economics_verified,double_collection_finality_verified)
                           VALUES($1,$2,$3,$4,$5)
                           RETURNING position_id""",
                        funding_set.position_id,
                        funding_set.total_count,
                        funding_set.funding_digest_sha256,
                        funding_set.economics_verified,
                        funding_set.double_collection_finality_verified,
                    )
                    if inserted is None:
                        raise VerifiedFundingLedgerHalted(
                            "VERIFIED_FUNDING_SET_INSERT_FAILED"
                        )
                    for event in funding_set.events:
                        row = await conn.fetchval(
                            """INSERT INTO t100_live_funding_events
                               (event_id,position_id,symbol,position_type,
                                position_value,funding_cashflow_usdt,rate,
                                settle_time_ms)
                               VALUES($1,$2,$3,$4,$5,$6,$7,$8)
                               RETURNING event_id""",
                            event.event_id,
                            event.position_id,
                            event.symbol,
                            event.position_type,
                            event.position_value,
                            event.funding_cashflow_usdt,
                            event.rate,
                            event.settle_time_ms,
                        )
                        if row is None:
                            raise VerifiedFundingLedgerHalted(
                                "VERIFIED_FUNDING_EVENT_INSERT_FAILED"
                            )
                    return "PERSISTED"
        except VerifiedFundingLedgerHalted:
            raise
        except Exception as exc:
            raise VerifiedFundingLedgerHalted(
                "VERIFIED_FUNDING_TRANSACTION_FAILED"
            ) from exc

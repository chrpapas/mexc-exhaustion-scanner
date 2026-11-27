"""Read-only proof of V40's IMMUTABLE before-POST contract terms.

The exact multiplier, tick and lot are atomically inserted by the fenced
four-slot attempt journal before SUBMITTING. This adapter reads the same row
in two independent REPEATABLE READ / READ ONLY PostgreSQL transactions.
It does NOT certify a historical time series of contract changes, final
accounting persistence, or live exchange execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal as D, ROUND_CEILING
import re
from typing import Any

from app.t100_live_durable_recovery import PositionCheckpoint


class V40EntryContractArchiveHalted(RuntimeError):
    """Finite codes only; do not surface SQL errors, rows or credentials."""


AT_ENTRY_SPEC_SQL = """
SELECT signal_id,external_oid,symbol,contract_size,volume_step,price_tick,
       reference_price,notional_usdt,volume_contracts,stop_price,
       ledger_sha256,inventory_sha256,captured_at
FROM public.t100_live_v40_entry_contract_specs
WHERE external_oid=$1
"""


@dataclass(frozen=True)
class V40ArchivedEntryContractSpec:
    signal_id: int
    external_oid: str
    symbol: str
    contract_size: D
    volume_step: D
    price_tick: D
    reference_price: D
    notional_usdt: D
    volume_contracts: D
    initial_stop_price: D
    ledger_sha256: str
    inventory_sha256: str
    captured_at: datetime
    read_twice_verified: bool = False
    live_authorization: bool = False
    mexc_posts: int = 0
    database_writes: int = 0


def _positive(value: Any) -> D:
    if type(value) is bool:
        raise V40EntryContractArchiveHalted("V40_ENTRY_ARCHIVE_DECIMAL_INVALID")
    try:
        x=D(str(value))
    except Exception:
        raise V40EntryContractArchiveHalted("V40_ENTRY_ARCHIVE_DECIMAL_INVALID") from None
    if not x.is_finite() or x<=0:
        raise V40EntryContractArchiveHalted("V40_ENTRY_ARCHIVE_DECIMAL_INVALID")
    return x


def _decode(row: dict[str,Any]) -> V40ArchivedEntryContractSpec:
    if (
        type(row) is not dict
        or type(row.get("signal_id")) is not int
        or row["signal_id"]<=0
        or type(row.get("external_oid")) is not str
        or not row["external_oid"]
        or type(row.get("symbol")) is not str
        or re.fullmatch(r"[A-Z0-9]+_USDT",row["symbol"]) is None
        or type(row.get("captured_at")) is not datetime
        or row["captured_at"].tzinfo is None
        or row["captured_at"].utcoffset() is None
        or any(type(row.get(k)) is not str
               or re.fullmatch(r"[a-f0-9]{64}",row[k]) is None
               for k in ("ledger_sha256","inventory_sha256"))
    ):
        raise V40EntryContractArchiveHalted(
            "V40_ENTRY_ARCHIVE_ORIGIN_OR_CLOCK_INVALID"
        )
    nums={}
    for name in (
        "contract_size","volume_step","price_tick","reference_price",
        "notional_usdt","volume_contracts","stop_price",
    ):
        nums[name]=_positive(row.get(name))
    if (
        nums["reference_price"]*nums["contract_size"]*
            nums["volume_contracts"]!=nums["notional_usdt"]
        or nums["volume_contracts"]%nums["volume_step"]!=0
        or nums["stop_price"]!=(
            nums["reference_price"]*D("1.75")/nums["price_tick"]
        ).to_integral_value(rounding=ROUND_CEILING)*nums["price_tick"]
        or row["captured_at"].astimezone(timezone.utc)>
            datetime.now(timezone.utc)
    ):
        raise V40EntryContractArchiveHalted(
            "V40_ENTRY_ARCHIVE_LOT_SL75_OR_CLOCK_MISMATCH"
        )
    return V40ArchivedEntryContractSpec(
        signal_id=row["signal_id"],
        external_oid=row["external_oid"],
        symbol=row["symbol"],
        contract_size=nums["contract_size"],
        volume_step=nums["volume_step"],
        price_tick=nums["price_tick"],
        reference_price=nums["reference_price"],
        notional_usdt=nums["notional_usdt"],
        volume_contracts=nums["volume_contracts"],
        initial_stop_price=nums["stop_price"],
        ledger_sha256=row["ledger_sha256"],
        inventory_sha256=row["inventory_sha256"],
        captured_at=row["captured_at"],
        read_twice_verified=True,
    )


class PostgresV40AtEntryContractSpecReader:
    def __init__(self, pool: Any):
        if pool is None or not callable(getattr(pool,"acquire",None)):
            raise V40EntryContractArchiveHalted(
                "V40_ENTRY_ARCHIVE_RESTRICTED_POOL_REQUIRED"
            )
        self.pool=pool

    async def read(
        self, previous: PositionCheckpoint,
    ) -> V40ArchivedEntryContractSpec:
        if (
            type(previous) is not PositionCheckpoint
            or previous.lifecycle_state!="OPEN"
            or type(previous.external_oid) is not str
            or not previous.external_oid
            or type(previous.contracts) is not D
            or not previous.contracts.is_finite()
            or previous.contracts<=0
        ):
            raise V40EntryContractArchiveHalted(
                "V40_ENTRY_ARCHIVE_PRIOR_CHECKPOINT_REQUIRED"
            )
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction(
                    isolation="repeatable_read",readonly=True,
                ):
                    first=await conn.fetchrow(
                        AT_ENTRY_SPEC_SQL,previous.external_oid,
                    )
                async with conn.transaction(
                    isolation="repeatable_read",readonly=True,
                ):
                    second=await conn.fetchrow(
                        AT_ENTRY_SPEC_SQL,previous.external_oid,
                    )
            if first is None or second is None:
                raise V40EntryContractArchiveHalted(
                    "V40_ENTRY_ARCHIVE_MISSING_BEFORE_POST_SPEC"
                )
            a,b=dict(first),dict(second)
            if a!=b:
                raise V40EntryContractArchiveHalted(
                    "V40_ENTRY_ARCHIVE_CHANGED_BETWEEN_TRANSACTIONS"
                )
            spec=_decode(b)
            if (
                spec.external_oid!=previous.external_oid
                or spec.symbol!=previous.symbol
                or spec.volume_contracts!=previous.contracts
            ):
                raise V40EntryContractArchiveHalted(
                    "V40_ENTRY_ARCHIVE_CHECKPOINT_OR_QUANTITY_MISMATCH"
                )
            return spec
        except V40EntryContractArchiveHalted:
            raise
        except Exception:
            raise V40EntryContractArchiveHalted(
                "V40_ENTRY_ARCHIVE_READONLY_SQL_UNVERIFIED"
            ) from None

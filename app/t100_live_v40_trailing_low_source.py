"""Conservative completed-Min30 low witness for V40 trailing, GET-free.

A market fill in the middle of a candle must NOT inherit that candle's low
because the low might have occurred *before* the short existed. Only bars
whose open is >= the exact entry-fill timestamp are included. Every bar
must be complete at the observation cutoff; gaps and stale history HALT.

The original 14% activation / 0.5pp trailing algorithm is in
t100_live_trailing.py. This adapter supplies *causal Min30 data only*.
It cannot establish signed exchange-fill provenance, native stop security,
intrabar path, continuity of streaming lows or permission to amend an SL.

Reads restricted PostgreSQL in one repeatable-read read-only transaction.
No orders, migrations, stop amendments or strategy state writes.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime,timezone
from decimal import Decimal
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_v40_admission import BAR_SECONDS

MAX_HISTORICAL_BARS = 12000

LOW_BARS_SQL = """
SELECT open_time,open,high,low,close
FROM public.candles
WHERE symbol=$1 AND interval='Min30'
  AND open_time >= $2 AND open_time < $3
ORDER BY open_time
LIMIT $4
"""


class V40TrailingLowHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class V40CausalLow:
    status: str
    symbol: str
    first_completed_open_ts: int
    last_completed_open_ts: int
    complete_bars: int
    observed_low: Decimal | None
    covers_since_entry_fill: bool = False
    signed_fill_provenance_verified: bool = False
    intrabar_path_verified: bool = False
    stop_amendment_authorized: bool = False


def _aware(ts: datetime) -> datetime:
    if (
        type(ts) is not datetime
        or ts.tzinfo is None or ts.utcoffset() is None
    ):
        raise V40TrailingLowHalted("V40_TRAILING_TIMESTAMP_NAIVE")
    return ts.astimezone(timezone.utc)


def _strict_candle_value(value: Any) -> Decimal:
    try:
        d=Decimal(str(value))
    except (TypeError,ValueError,ArithmeticError):
        raise V40TrailingLowHalted("V40_TRAILING_CANDLE_VALUE_INVALID") from None
    if not d.is_finite() or d<=0:
        raise V40TrailingLowHalted("V40_TRAILING_CANDLE_VALUE_INVALID")
    return d


def inspect_completed_causal_lows(
    *,
    symbol: str, filled_at: datetime, observed_at: datetime,
    rows: list[dict[str,Any]],
) -> V40CausalLow:
    if (
        type(symbol) is not str
        or re.fullmatch(r"[A-Z0-9]+_USDT",symbol) is None
        or symbol in ("BTC_USDT","ETH_USDT")
        or type(rows) is not list
    ):
        raise V40TrailingLowHalted("V40_TRAILING_SOURCE_IDENTITY_INVALID")
    fill=_aware(filled_at)
    observed=_aware(observed_at)
    fill_seconds=(fill-datetime(1970,1,1,tzinfo=timezone.utc)).total_seconds()
    cutoff_seconds=(observed-datetime(1970,1,1,tzinfo=timezone.utc)).total_seconds()
    if (
        not math.isfinite(fill_seconds)
        or not math.isfinite(cutoff_seconds)
        or cutoff_seconds <= fill_seconds
    ):
        raise V40TrailingLowHalted("V40_TRAILING_FILL_OR_OBSERVATION_TIME_INVALID")

    # Only candles whose opening is not before the latest signed fill,
    # so the whole high/low range is causally after the position was open.
    first=int(math.ceil(fill_seconds/BAR_SECONDS))*BAR_SECONDS
    last_complete_exclusive=int(math.floor(cutoff_seconds/BAR_SECONDS))*BAR_SECONDS
    expected=max(0,(last_complete_exclusive-first)//BAR_SECONDS)
    if expected>MAX_HISTORICAL_BARS or len(rows)>MAX_HISTORICAL_BARS:
        raise V40TrailingLowHalted("V40_TRAILING_HISTORY_WINDOW_TOO_LARGE")
    if expected==0:
        if rows:
            raise V40TrailingLowHalted("V40_TRAILING_FUTURE_OR_UNCOMPLETED_CANDLE")
        return V40CausalLow(
            status="V40_NO_CAUSAL_COMPLETED_BARS_NONAUTHORIZING",
            symbol=symbol,first_completed_open_ts=first,
            last_completed_open_ts=first-BAR_SECONDS,
            complete_bars=0,observed_low=None,
        )
    if len(rows)!=expected:
        raise V40TrailingLowHalted("V40_TRAILING_COMPLETED_HISTORY_INCOMPLETE")

    lows=[]
    for i,row in enumerate(rows):
        if type(row) is not dict:
            raise V40TrailingLowHalted("V40_TRAILING_CANDLE_SHAPE_INVALID")
        open_at=row.get("open_time")
        if type(open_at) is not datetime:
            raise V40TrailingLowHalted("V40_TRAILING_CANDLE_TIME_INVALID")
        when=_aware(open_at)
        ts=when.timestamp()
        if ts!=first+i*BAR_SECONDS:
            raise V40TrailingLowHalted("V40_TRAILING_GAP_OR_LOOKAHEAD")
        op,hi,lo,cl=(
            _strict_candle_value(row.get(k))
            for k in ("open","high","low","close")
        )
        if lo>min(op,cl) or hi<max(op,cl):
            raise V40TrailingLowHalted("V40_TRAILING_OHLC_INVALID")
        lows.append(lo)
    return V40CausalLow(
        status="V40_COMPLETED_MIN30_CAUSAL_LOW_NONAUTHORIZING",
        symbol=symbol,
        first_completed_open_ts=first,
        last_completed_open_ts=first+(expected-1)*BAR_SECONDS,
        complete_bars=expected,
        observed_low=min(lows),
    )


class PostgresV40TrailingLowReader:
    def __init__(self,pool:Any):
        if pool is None:
            raise ValueError("V40_TRAILING_POOL_MISSING")
        self.pool=pool

    async def read(
        self, *, symbol: str, filled_at: datetime,
        observed_at: datetime,
    ) -> V40CausalLow:
        fill=_aware(filled_at)
        observed=_aware(observed_at)
        # Use an independent bounded SQL witness and complete-bar floor.
        fill_seconds=(fill-datetime(1970,1,1,tzinfo=timezone.utc)).total_seconds()
        cutoff_seconds=(observed-datetime(1970,1,1,tzinfo=timezone.utc)).total_seconds()
        first=int(math.ceil(fill_seconds/BAR_SECONDS))*BAR_SECONDS
        end=int(math.floor(cutoff_seconds/BAR_SECONDS))*BAR_SECONDS
        if first>=end:
            return inspect_completed_causal_lows(
                symbol=symbol,filled_at=fill,observed_at=observed,rows=[],
            )
        expected=(end-first)//BAR_SECONDS
        if expected>MAX_HISTORICAL_BARS:
            raise V40TrailingLowHalted("V40_TRAILING_HISTORY_WINDOW_TOO_LARGE")
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction(
                    isolation="repeatable_read",readonly=True,
                ):
                    if (
                        await conn.fetchval(
                            "SELECT current_setting('transaction_read_only')"
                        )!="on"
                        or await conn.fetchval(
                            "SELECT current_setting('transaction_isolation')"
                        )!="repeatable read"
                        or await conn.fetchval("SELECT current_database()")
                        !=EXPECTED_DATABASE
                        or await conn.fetchval("SELECT current_user")
                        !=TARGET_ROLE
                    ):
                        raise V40TrailingLowHalted(
                            "V40_TRAILING_RESTRICTED_READONLY_ROLE_REQUIRED"
                        )
                    bars=await conn.fetch(
                        LOW_BARS_SQL,symbol,
                        datetime.fromtimestamp(first,timezone.utc),
                        datetime.fromtimestamp(end,timezone.utc),
                        MAX_HISTORICAL_BARS+1,
                    )
                    if type(bars) not in (list,tuple):
                        raise V40TrailingLowHalted("V40_TRAILING_QUERY_SHAPE_INVALID")
                    return inspect_completed_causal_lows(
                        symbol=symbol,filled_at=fill,
                        observed_at=observed,
                        rows=[dict(x) for x in bars],
                    )
        except V40TrailingLowHalted:
            raise
        except Exception:
            raise V40TrailingLowHalted(
                "V40_TRAILING_READONLY_POSTGRES_QUERY_FAILED"
            ) from None

"""Strict read-only Stage2 -> frozen V40 signal intake from production PostgreSQL.

The paper scanner creates t100_p2_signals. Its paper trader later mutates
trade_decision; live candidate discovery MUST NOT filter on that field,
update it, or reuse paper t100_positions / synthetic fills.

One REPEATABLE READ, READ ONLY, role-pinned transaction retrieves a bounded
slice of eligible stage2 events AND their exactly 49 *prior completed*
Min30 candles. Any missing data, stale/unaligned signal, changed strategy,
incorrect Stage1 tier, price-anchor mismatch or ambiguous pagination HALTS
the entire batch; never silently trade a partially validated signal set.

This is a source adapter for a future V40 executor, not an executable
strategy runtime. It does not use exchange APIs, credentials, live gates,
trading permits, migrations or SQL mutations.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.t100_200_strategy import STRATEGY_ID
from app.t100_v40_admission import BAR_SECONDS, PriorBar, causal_49_bar_features
from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE

MAX_BATCH = 32
MAX_SIGNAL_AGE_SECONDS = 900
# The live-executor runtime role is deliberately DENIED SELECT on the
# scanner's paper/production signal and candle tables. A manually-run
# research CLI may use the existing scanner OWNER login only inside a
# forced READ ONLY, REPEATABLE READ transaction; this mode never provides
# live execution authority or a production source role.
MANUAL_SCANNER_READONLY_OWNER_ROLE = "mexc_scanner"

# The ordering includes the immutable signal PK, not mutable paper decisions.
# Query an extra row so exceeding the batch cap blocks instead of truncating.
SIGNALS_SQL = """
SELECT id,strategy_id,symbol,p2_at,feature_at,entry_price,
       physical_episode_seq,stage_no,stage1_return_24h,
       stage1_atr7_pct,stage1_strict365,stage1_eligible,
       t100_tier,eligible_stage2,created_at,trade_decision
FROM public.t100_p2_signals
WHERE eligible_stage2=true
  AND p2_at >= $1
  AND p2_at <= $2
ORDER BY p2_at,id
LIMIT $3
"""

HISTORY_SQL = """
SELECT open_time,high,close,amount
FROM public.candles
WHERE symbol=$1
  AND interval='Min30'
  AND open_time < $2
ORDER BY open_time DESC
LIMIT 49
"""


class V40SignalIntakeHalted(RuntimeError):
    """Never embed SQL error text, credentials or raw rows."""


@dataclass(frozen=True)
class V40SourceCandidate:
    signal_id: int
    strategy_id: str
    symbol: str
    admission_open_ts: int
    feature_open_ts: int
    entry_reference_price: float
    stage1_atr7_pct: float
    stage1_return_24h: float
    physical_episode_seq: int
    tier: str
    prior_bars: tuple[PriorBar, ...]
    pre24_vol_pct: float
    observed_age_seconds: float
    # A source check is not account / margin / native-stop readiness.
    entry_allowed: bool = False
    live_authorization: bool = False


@dataclass(frozen=True)
class V40SourceBatch:
    status: str
    observed_at: datetime
    candidates: tuple[V40SourceCandidate, ...]
    read_only_repeatable_read: bool = True
    history_complete: bool = True
    # No POST authorization is conferred by this data source.
    entry_allowed: bool = False
    live_authorization: bool = False


def _timestamp(value: Any) -> int:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise V40SignalIntakeHalted("SOURCE_NAIVE_TIMESTAMP")
    return int(value.timestamp())


def _exact_candle_timestamp(value: Any) -> int:
    ts = _timestamp(value)
    if value.microsecond != 0 or value.timestamp() != ts:
        raise V40SignalIntakeHalted("SOURCE_SUBSECOND_CANDLE_CLOCK_INVALID")
    return ts


def _positive_float(value: Any) -> float:
    if type(value) is bool or type(value) not in (int, float):
        raise V40SignalIntakeHalted("SOURCE_INVALID_NUMBER")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise V40SignalIntakeHalted("SOURCE_INVALID_NUMBER")
    return result


def inspect_stage2_candidate(
    signal: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    observed_at: datetime,
) -> V40SourceCandidate:
    """Validate source provenance without inventing bars or risking a POST."""
    observed_ts = _timestamp(observed_at)
    if not isinstance(signal, dict) or type(history) is not list:
        raise V40SignalIntakeHalted("SOURCE_SIGNAL_OR_HISTORY_SHAPE_INVALID")
    try:
        identifier = signal["id"]
        symbol = signal["symbol"]
        p2_at = _exact_candle_timestamp(signal["p2_at"])
        feature_at = _exact_candle_timestamp(signal["feature_at"])
        created_at = _timestamp(signal["created_at"])
        episode = signal["physical_episode_seq"]
        tier = signal["t100_tier"]
        entry = _positive_float(signal["entry_price"])
        atr = _positive_float(signal["stage1_atr7_pct"])
        r24 = _positive_float(signal["stage1_return_24h"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise V40SignalIntakeHalted("SOURCE_SIGNAL_FIELDS_MISSING_OR_INVALID") from None

    if (
        type(identifier) is not int or identifier <= 0
        or type(symbol) is not str
        or re.fullmatch(r"[A-Z0-9]+_USDT", symbol) is None
        or symbol in ("BTC_USDT", "ETH_USDT")
        or signal.get("strategy_id") != STRATEGY_ID
        or type(episode) is not int or episode <= 0
        or type(signal.get("stage_no")) is not int
        or signal["stage_no"] != 2
        or signal.get("stage1_strict365") is not True
        or signal.get("stage1_eligible") is not True
        or signal.get("eligible_stage2") is not True
        or r24 < 0.15 or atr < 4.0
        or tier != ("HIGH_225" if atr >= 5.7 else "LOW_100")
    ):
        raise V40SignalIntakeHalted("SOURCE_STAGE2_CONTRACT_OR_TIER_MISMATCH")

    age = observed_ts - p2_at
    if (
        p2_at % BAR_SECONDS
        or feature_at != p2_at-BAR_SECONDS
        or created_at < p2_at
        or created_at > observed_ts
        or age < 0 or age > MAX_SIGNAL_AGE_SECONDS
    ):
        raise V40SignalIntakeHalted("SOURCE_SIGNAL_CLOCK_OR_AGE_INVALID")

    if len(history) != 49:
        raise V40SignalIntakeHalted("SOURCE_CAUSAL_49_BAR_COUNT_INVALID")
    bars: list[PriorBar] = []
    seen: set[int] = set()
    for raw in history:
        if not isinstance(raw, dict):
            raise V40SignalIntakeHalted("SOURCE_CANDLE_SHAPE_INVALID")
        try:
            open_ts = _exact_candle_timestamp(raw["open_time"])
            high = _positive_float(raw["high"])
            close = _positive_float(raw["close"])
            amount = _positive_float(raw["amount"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise V40SignalIntakeHalted("SOURCE_CANDLE_FIELDS_INVALID") from None
        if open_ts in seen or close > high:
            raise V40SignalIntakeHalted("SOURCE_CANDLE_DUPLICATE_OR_OHLC_INVALID")
        seen.add(open_ts)
        bars.append(PriorBar(open_ts,high,close,amount))

    ordered = tuple(sorted(bars,key=lambda x:x.open_ts))
    features = causal_49_bar_features(ordered,admit_open_ts=p2_at)
    if features is None:
        raise V40SignalIntakeHalted("SOURCE_CAUSAL_49_BAR_TIMING_INVALID")
    # The scanner uses the completed Min30 close as entry reference. A
    # different source anchor invalidates entry sizing and stop levels.
    if abs(ordered[-1].close-entry) > max(1e-8, entry*1e-6):
        raise V40SignalIntakeHalted("SOURCE_ENTRY_REFERENCE_PRICE_DRIFT")

    return V40SourceCandidate(
        signal_id=identifier, strategy_id=STRATEGY_ID,
        symbol=symbol, admission_open_ts=p2_at,
        feature_open_ts=feature_at, entry_reference_price=entry,
        stage1_atr7_pct=atr, stage1_return_24h=r24,
        physical_episode_seq=episode, tier=tier,
        prior_bars=ordered, pre24_vol_pct=float(features[0]),
        observed_age_seconds=float(age),
    )


class PostgresV40Stage2Source:
    """Account-unprivileged production signal reader; no trade_decision writes."""

    def __init__(
        self, pool: Any, *,
        manual_scanner_owner_readonly: bool = False,
    ):
        if pool is None or type(manual_scanner_owner_readonly) is not bool:
            raise ValueError("V40_SIGNAL_READER_POOL_MISSING")
        self.pool = pool
        # Explicit, opt-in manual research only; default contract is
        # unchanged. No way to select an arbitrary high-privilege role.
        self.expected_source_role = (
            MANUAL_SCANNER_READONLY_OWNER_ROLE
            if manual_scanner_owner_readonly else TARGET_ROLE
        )
        self.manual_scanner_owner_readonly = manual_scanner_owner_readonly

    async def latest_candidates(
        self, *,
        observed_at: datetime,
        max_age_seconds: int = MAX_SIGNAL_AGE_SECONDS,
        limit: int = MAX_BATCH,
    ) -> V40SourceBatch:
        if (
            type(observed_at) is not datetime
            or observed_at.tzinfo is None
            or observed_at.utcoffset() is None
            or type(max_age_seconds) is not int
            or not 1 <= max_age_seconds <= MAX_SIGNAL_AGE_SECONDS
            or type(limit) is not int
            or not 1 <= limit <= MAX_BATCH
        ):
            raise V40SignalIntakeHalted("SOURCE_INTAKE_ARGS_INVALID")
        observed_at = observed_at.astimezone(timezone.utc)
        boundary = observed_at - timedelta(seconds=max_age_seconds)
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction(
                    isolation="repeatable_read",readonly=True,
                ):
                    if (
                        await conn.fetchval(
                            "SELECT current_setting('transaction_read_only')"
                        ) != "on"
                        or await conn.fetchval(
                            "SELECT current_setting('transaction_isolation')"
                        ) != "repeatable read"
                        or await conn.fetchval("SELECT current_database()")
                        != EXPECTED_DATABASE
                        or await conn.fetchval("SELECT current_user")
                        != self.expected_source_role
                    ):
                        raise V40SignalIntakeHalted(
                            "SOURCE_RESTRICTED_REPEATABLE_READ_CONTEXT_REQUIRED"
                        )
                    signal_rows = await conn.fetch(
                        SIGNALS_SQL,boundary,observed_at,limit+1,
                    )
                    if not isinstance(signal_rows,(list,tuple)):
                        raise V40SignalIntakeHalted(
                            "SOURCE_SIGNAL_QUERY_SHAPE_INVALID"
                        )
                    if len(signal_rows)>limit:
                        raise V40SignalIntakeHalted(
                            "SOURCE_SIGNAL_BATCH_TRUNCATION_BLOCKED"
                        )
                    candidates: list[V40SourceCandidate] = []
                    seen_ids: set[int] = set()
                    seen_origin: set[tuple[str,int]] = set()
                    for row in signal_rows:
                        signal = dict(row)
                        symbol = signal.get("symbol")
                        time = signal.get("p2_at")
                        if type(symbol) is not str or type(time) is not datetime:
                            raise V40SignalIntakeHalted(
                                "SOURCE_SIGNAL_IDENTITY_INVALID"
                            )
                        candle_rows = await conn.fetch(
                            HISTORY_SQL,symbol,time,
                        )
                        if not isinstance(candle_rows,(list,tuple)):
                            raise V40SignalIntakeHalted(
                                "SOURCE_HISTORY_QUERY_SHAPE_INVALID"
                            )
                        item = inspect_stage2_candidate(
                            signal,[dict(x) for x in candle_rows],
                            observed_at=observed_at,
                        )
                        origin=(item.symbol,item.admission_open_ts)
                        if item.signal_id in seen_ids or origin in seen_origin:
                            raise V40SignalIntakeHalted(
                                "SOURCE_DUPLICATE_SIGNAL_OR_SYMBOL_TIME"
                            )
                        seen_ids.add(item.signal_id)
                        seen_origin.add(origin)
                        candidates.append(item)
            return V40SourceBatch(
                status="FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING",
                observed_at=observed_at,
                candidates=tuple(candidates),
            )
        except V40SignalIntakeHalted:
            raise
        except Exception:
            # Raw DB error strings can expose SQL, host, names and credentials.
            raise V40SignalIntakeHalted(
                "SOURCE_READ_ONLY_POSTGRES_QUERY_FAILED"
            ) from None

"""Historical, NON-TRADABLE V40 Stage2-to-Min30 causal integrity audit.

Reads a bounded recent historical sample of actual scanner Stage2 signals
and their exact 49 *prior* completed Min30 candles, under one scanner-owner
READ ONLY, REPEATABLE READ transaction. Reuses the frozen live candidate
validator with an explicit historical AS-OF event-window clock, never the
current (stale) clock. A signal created >15 minutes after its event remains
invalid; it cannot be repaired by historical replay.

THIS IS RESEARCH ONLY: no object from this module is a V40SourceCandidate
consumable by entry coordinators; no private/public MEXC GET or POST; no
account credentials, Keychain, DB writes, order/stop/permission changes,
or automated trading authorization. It does not prove scanner uptime,
market liquidity, slippage, signal profitability, or current eligibility.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_v40_stage2_source import (
    HISTORY_SQL,MAX_SIGNAL_AGE_SECONDS,MANUAL_SCANNER_READONLY_OWNER_ROLE,
    PostgresV40Stage2Source,V40SignalIntakeHalted,inspect_stage2_candidate,
)

MAX_HISTORICAL_RECORDS=8
MAX_LOOKBACK_DAYS=7
HISTORICAL_SQL="""
SELECT id,strategy_id,symbol,p2_at,feature_at,entry_price,
       physical_episode_seq,stage_no,stage1_return_24h,
       stage1_atr7_pct,stage1_strict365,stage1_eligible,
       t100_tier,eligible_stage2,created_at,trade_decision
FROM public.t100_p2_signals
WHERE eligible_stage2=true
  AND p2_at >= $1 AND p2_at <= $2
ORDER BY p2_at DESC,id DESC
LIMIT $3
"""

# Only known source-origin error codes are admissible in a shared report.
# Raw database exception text, SQL, symbols with noncanonical identities,
# credentials and untrusted exceptions are never surfaced.
KNOWN_REASONS=frozenset({
    "SOURCE_NAIVE_TIMESTAMP",
    "SOURCE_SUBSECOND_CANDLE_CLOCK_INVALID",
    "SOURCE_INVALID_NUMBER",
    "SOURCE_SIGNAL_OR_HISTORY_SHAPE_INVALID",
    "SOURCE_SIGNAL_FIELDS_MISSING_OR_INVALID",
    "SOURCE_STAGE2_CONTRACT_OR_TIER_MISMATCH",
    "SOURCE_SIGNAL_CLOCK_OR_AGE_INVALID",
    "SOURCE_CAUSAL_49_BAR_COUNT_INVALID",
    "SOURCE_CANDLE_SHAPE_INVALID",
    "SOURCE_CANDLE_FIELDS_INVALID",
    "SOURCE_CANDLE_DUPLICATE_OR_OHLC_INVALID",
    "SOURCE_CAUSAL_49_BAR_TIMING_INVALID",
    "SOURCE_ENTRY_REFERENCE_PRICE_DRIFT",
})


@dataclass(frozen=True)
class HistoricalSignalAssessment:
    # Not a V40SourceCandidate. Cannot be passed to strategy/admission.
    symbol: str|None
    signal_id: int|None
    event_time_utc: str|None
    status: str
    tier: str|None=None
    causal_49_bars_reconstructed: bool=False
    historical_only: bool=True
    entry_allowed: bool=False
    live_authorization: bool=False


@dataclass(frozen=True)
class HistoricalStage2Audit:
    status: str
    observed_at_utc: str|None=None
    checked_records: int=0
    reconstructed_records: int=0
    invalid_records: int=0
    max_records: int=MAX_HISTORICAL_RECORDS
    lookback_days: int=MAX_LOOKBACK_DAYS
    historical_sample_only: bool=True
    records: tuple[HistoricalSignalAssessment,...]=()
    fresh_signal_discovery_verified: bool=False
    scanner_uptime_verified: bool=False
    market_settings_verified: bool=False
    order_execution_verified: bool=False
    entry_allowed: bool=False
    live_authorization: bool=False
    mexc_posts: int=0
    database_writes: int=0
    read_only: bool=True


def _halt(reason:str)->HistoricalStage2Audit:
    return HistoricalStage2Audit(status=reason)


def _safe_event_identity(
    row:dict[str,Any],*,now:datetime,
)->tuple[int|None,str|None,str|None,datetime|None]:
    signal_id=row.get("id")
    if type(signal_id) is not int or signal_id<1:
        signal_id=None
    symbol=row.get("symbol")
    if (
        type(symbol) is not str
        or re.fullmatch(r"[A-Z0-9]+_USDT",symbol) is None
    ):
        symbol=None
    event=row.get("p2_at")
    if type(event) is not datetime or event.tzinfo is None or event.utcoffset() is None:
        return signal_id,symbol,None,None
    event=event.astimezone(timezone.utc)
    if event>now or event.year<2020:
        return signal_id,symbol,None,None
    return signal_id,symbol,event.isoformat(),event


async def inspect_historical_stage2(
    source:PostgresV40Stage2Source,*,observed_at:datetime,
    lookback_days:int=MAX_LOOKBACK_DAYS,
    max_records:int=MAX_HISTORICAL_RECORDS,
)->HistoricalStage2Audit:
    """Bounded historical audit, independent of the live candidate intake."""
    if (
        type(source) is not PostgresV40Stage2Source
        or source.manual_scanner_owner_readonly is not True
        or source.expected_source_role!=MANUAL_SCANNER_READONLY_OWNER_ROLE
        or type(observed_at) is not datetime
        or observed_at.tzinfo is None or observed_at.utcoffset() is None
        or type(lookback_days) is not int or not 1<=lookback_days<=MAX_LOOKBACK_DAYS
        or type(max_records) is not int or not 1<=max_records<=MAX_HISTORICAL_RECORDS
    ):
        return _halt("V40_HISTORICAL_RESEARCH_SCOPE_OR_LIMIT_INVALID_HALT")
    now=observed_at.astimezone(timezone.utc)
    if now.year<2020:
        return _halt("V40_HISTORICAL_CLOCK_INVALID_HALT")
    window_start=now-timedelta(days=lookback_days)
    try:
        async with source.pool.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read",readonly=True):
                if (
                    await conn.fetchval("SELECT current_setting('transaction_read_only')")!="on"
                    or await conn.fetchval("SELECT current_setting('transaction_isolation')")!="repeatable read"
                    or await conn.fetchval("SELECT current_database()")!=EXPECTED_DATABASE
                    or await conn.fetchval("SELECT current_user")!=MANUAL_SCANNER_READONLY_OWNER_ROLE
                ):
                    return _halt("V40_HISTORICAL_READONLY_OWNER_IDENTITY_HALT")

                raw_rows=await conn.fetch(
                    HISTORICAL_SQL,window_start,now,max_records,
                )
                if (
                    not isinstance(raw_rows,(list,tuple))
                    or len(raw_rows)>max_records
                ):
                    return _halt("V40_HISTORICAL_BOUND_OR_SOURCE_SHAPE_HALT")
                results=[]
                seen_ids=set()
                seen_origins=set()
                for raw in raw_rows:
                    try:
                        row=dict(raw)
                    except Exception:
                        return _halt("V40_HISTORICAL_ROW_UNREADABLE_HALT")
                    sid,symbol,iso,at=_safe_event_identity(row,now=now)
                    if (
                        sid is None or sid in seen_ids
                        or symbol is None or at is None
                        or at<window_start
                        or (symbol,at) in seen_origins
                    ):
                        return _halt("V40_HISTORICAL_IDENTITY_OR_WINDOW_INVALID_HALT")
                    seen_ids.add(sid)
                    seen_origins.add((symbol,at))
                    # HISTORY_SQL remains strictly prior-completed bars,
                    # never current/hindsight candles.
                    candle_rows=await conn.fetch(HISTORY_SQL,symbol,at)
                    if not isinstance(candle_rows,(list,tuple)):
                        return _halt("V40_HISTORICAL_CANDLE_READBACK_INVALID_HALT")
                    try:
                        # Historical clock is AS OF admission+15m, not the
                        # current clock. Still enforces the live validator's
                        # creation-timestamp and 49-bar causality rules.
                        item=inspect_stage2_candidate(
                            row,[dict(r) for r in candle_rows],
                            observed_at=at+timedelta(
                                seconds=MAX_SIGNAL_AGE_SECONDS,
                            ),
                        )
                    except V40SignalIntakeHalted as exc:
                        reason=str(exc)
                        results.append(HistoricalSignalAssessment(
                            symbol=symbol,signal_id=sid,
                            event_time_utc=iso,
                            status=(
                                reason if reason in KNOWN_REASONS else
                                "V40_HISTORICAL_UNRECOGNIZED_SOURCE_INVALID"
                            ),
                        ))
                    except Exception:
                        results.append(HistoricalSignalAssessment(
                            symbol=symbol,signal_id=sid,
                            event_time_utc=iso,
                            status="V40_HISTORICAL_UNRECOGNIZED_SOURCE_INVALID",
                        ))
                    else:
                        results.append(HistoricalSignalAssessment(
                            symbol=item.symbol,signal_id=item.signal_id,
                            event_time_utc=iso,
                            status="HISTORICAL_CAUSAL_49_BAR_RECONSTRUCTED",
                            tier=item.tier,
                            causal_49_bars_reconstructed=True,
                        ))
        reconstructed=sum(
            r.causal_49_bars_reconstructed is True for r in results
        )
        return HistoricalStage2Audit(
            status=(
                "V40_HISTORICAL_STAGE2_SAMPLE_EMPTY_NONAUTHORIZING"
                if not results else
                "V40_HISTORICAL_STAGE2_CAUSAL_SAMPLE_VALID_NONAUTHORIZING"
                if reconstructed==len(results) else
                "V40_HISTORICAL_STAGE2_CAUSAL_SAMPLE_GAPS_NONAUTHORIZING"
            ),
            observed_at_utc=now.isoformat(),
            checked_records=len(results),
            reconstructed_records=reconstructed,
            invalid_records=len(results)-reconstructed,
            max_records=max_records,lookback_days=lookback_days,
            records=tuple(results),
        )
    except Exception:
        return _halt("V40_HISTORICAL_READONLY_DB_UNAVAILABLE_HALT")

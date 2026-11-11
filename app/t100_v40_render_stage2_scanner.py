"""Render-bound V40 Stage2 signal producer + STRICT read-only V40 shadow filter.

Review-only cutover entrypoint for the EXISTING 'mexc-exhaustion-scanner'
background worker. The original Stage2 detection/candle ingestion is retained
because frozen V40 requires those *fresh* native P2/Stage2 events and 49
causal 30m candles. The retired paper portfolio/daily subscriber reporting
is disabled in this entrypoint.

Only the inherited scanner's normal candle/Stage2 writes occur. The V40
post-evaluation observer opens its own READ ONLY REPEATABLE READ transaction
and emits redacted counts, no order intents, no MEXC private GET or POST,
no stop changes, no cross-margin risk claims and NO live authorization.

This is an engineering candidate, NOT deployed. Do not use the scanner-owner
DB role or its credentials as a future LIVE EXECUTOR or watcher principal.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.t100_200_scanner import T100Scanner
from app.t100_200_strategy import STRATEGY_ID
from app.t100_200_worker import configure_logging
from app.t100_live_v40_stage2_source import (
    HISTORY_SQL,
    MAX_BATCH,
    MAX_SIGNAL_AGE_SECONDS,
    inspect_stage2_candidate,
)
from app.t100_v40_admission import VOL_SKIP_THRESHOLD

LOGGER = logging.getLogger(__name__)


class V40RenderCutoverHalted(RuntimeError):
    pass


def require_v40_shadow_render_guards(env: Any) -> None:
    """Refuse early unless explicitly disabled for ALL real execution."""
    if (
        not hasattr(env, "get")
        or env.get("T100_V40_RENDER_SCANNER_ENABLED") != "true"
        or env.get("T100_EXECUTION_MODE") != "paper"
        or env.get("T100_LIVE_ORDER_API_ENABLED") != "false"
        or env.get("T100_LIVE_KILL_SWITCH") != "true"
        or env.get("EXECUTION_ENABLED") != "false"
        or env.get("TRADING_MODE") != "paper"
    ):
        raise V40RenderCutoverHalted("V40_SHADOW_PAPER_KILL_CONTROLS_REQUIRED")


@dataclass(frozen=True)
class V40ObservedSignal:
    """Provenance from genuine DB Stage2 + causal 49-bar/VOL30 research gate.

    This receipt is **NOT** an order or an authorization for an executor.
    """
    signal_id: int
    symbol: str
    tier: str
    p2_at: datetime
    pre24_vol_pct: float


@dataclass(frozen=True)
class V40ShadowBatch:
    status: str
    validated_stage2: int = 0
    vol30_skip: int = 0
    vol30_passed_research_only: int = 0
    passed_signals: tuple[V40ObservedSignal, ...] = ()
    history_verified: bool = False
    source_readonly_transaction: bool = False
    source_is_scanner_owner_not_executor: bool = True
    signal_transport_to_executor_certified: bool = False
    exact_order_size_certified: bool = False
    account_risk_certified: bool = False
    native_stop_execution_certified: bool = False
    watchdog_running_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_posts: int = 0


V40_SHADOW_NEW_EVENTS_SQL = """
SELECT id,strategy_id,symbol,p2_at,feature_at,entry_price,
       physical_episode_seq,stage_no,stage1_return_24h,
       stage1_atr7_pct,stage1_strict365,stage1_eligible,
       t100_tier,eligible_stage2,created_at,trade_decision
FROM public.t100_p2_signals
WHERE eligible_stage2=true AND p2_at=$1
ORDER BY p2_at,id
LIMIT $2
"""


async def inspect_new_v40_stage2(
    pool: Any, *, eval_at: datetime, observed_at: datetime,
) -> V40ShadowBatch:
    """Pure observation of just-generated Stage2, no trading decisions.

    Any unvalidated signal rejects the WHOLE batch. Never infer live
    eligibility from the paper trader's mutable trade_decision column.
    """
    if (
        not isinstance(eval_at, datetime)
        or not isinstance(observed_at, datetime)
        or eval_at.utcoffset() is None
        or observed_at.utcoffset() is None
    ):
        return V40ShadowBatch("V40_SHADOW_INVALID_EVALUATION_CLOCK")
    age=(observed_at-eval_at).total_seconds()
    if age < 0 or age > MAX_SIGNAL_AGE_SECONDS:
        # Completed historic catch-up scans are never fresh live entries.
        return V40ShadowBatch("V40_SHADOW_EVAL_NOT_CURRENT_NO_LIVE_CANDIDATE")
    try:
        async with pool.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read",readonly=True):
                if (
                    await conn.fetchval("SELECT current_setting('transaction_read_only')")!="on"
                    or await conn.fetchval("SELECT current_setting('transaction_isolation')")!="repeatable read"
                    or await conn.fetchval("SELECT current_database()")!="mexc_exhaustion"
                    or await conn.fetchval("SELECT current_user")!="mexc_scanner"
                ):
                    return V40ShadowBatch("V40_SHADOW_DB_RESTRICTED_READ_CONTEXT_INVALID")
                rows=await conn.fetch(
                    V40_SHADOW_NEW_EVENTS_SQL,eval_at,MAX_BATCH+1,
                )
                if type(rows) not in (list,tuple) or len(rows)>MAX_BATCH:
                    return V40ShadowBatch("V40_SHADOW_SOURCE_BATCH_LIMIT_OR_SHAPE_INVALID")
                validated=0
                skipped=0
                passed=0
                accepted=[]
                origin_ids=set()
                for row in rows:
                    signal=dict(row)
                    sid=signal.get("id")
                    if type(sid) is not int or sid in origin_ids:
                        return V40ShadowBatch("V40_SHADOW_DUPLICATE_OR_BAD_SOURCE_ID")
                    origin_ids.add(sid)
                    candle_rows=await conn.fetch(
                        HISTORY_SQL,signal.get("symbol"),signal.get("p2_at"),
                    )
                    candidate=inspect_stage2_candidate(
                        signal,[dict(x) for x in candle_rows],
                        observed_at=observed_at,
                    )
                    validated+=1
                    if candidate.pre24_vol_pct>=VOL_SKIP_THRESHOLD:
                        skipped+=1
                    else:
                        passed+=1
                        accepted.append(V40ObservedSignal(
                            signal_id=candidate.signal_id,
                            symbol=candidate.symbol,
                            tier=candidate.tier,
                            p2_at=signal["p2_at"],
                            pre24_vol_pct=candidate.pre24_vol_pct,
                        ))
                return V40ShadowBatch(
                    "V40_SHADOW_FRESH_STAGE2_CAUSAL_VOL30_AUDIT_NONAUTHORIZING",
                    validated_stage2=validated,
                    vol30_skip=skipped,
                    vol30_passed_research_only=passed,
                    passed_signals=tuple(accepted),
                    history_verified=True,
                    source_readonly_transaction=True,
                )
    except (Exception,):
        # No SQL error/URL, account or symbol values can leak to logs.
        return V40ShadowBatch("V40_SHADOW_SIGNAL_OR_HISTORY_VALIDATION_HALTED")


class V40Stage2RenderScanner(T100Scanner):
    """Keeps source generation; replaces old dashboard with V40 shadow gate."""

    async def initialize(self) -> None:
        """No automatic migrations or paper-runtime creation on V40 cutover.

        Original scanner.initialize() invokes db.migrate() and an INSERT.
        A future integration branch can contain migrations not yet reviewed
        against production. Require the existing scanner schema and identity,
        then do ONLY the regular source scanner's market-data maintenance.
        """
        try:
            await self.db.connect()
            async with self.db.pool.acquire() as conn:
                identity=await conn.fetchrow(
                    "SELECT current_database() AS db, current_user AS role"
                )
                runtime=await conn.fetchrow(
                    "SELECT strategy_id,run_id "
                    "FROM public.t100_runtime WHERE singleton=true"
                )
                if (
                    identity is None or runtime is None
                    or identity["db"]!="mexc_exhaustion"
                    or identity["role"]!="mexc_scanner"
                    or runtime["strategy_id"]!=STRATEGY_ID
                ):
                    raise V40RenderCutoverHalted(
                        "V40_SCANNER_EXISTING_SCHEMA_OR_ROLE_INVALID"
                    )
            await self.refresh_contracts()
        except V40RenderCutoverHalted:
            raise
        except Exception:
            # Never expose database credentials or SQL errors in a log.
            raise V40RenderCutoverHalted(
                "V40_SCANNER_EXISTING_SCHEMA_READ_FAILED"
            ) from None

    async def _maybe_send_daily_dashboard(self, now: datetime) -> None:
        # Deliberately no old T100_200 portfolio/report side effects.
        return None

    async def process_eval(self, eval_at: datetime) -> None:
        await super().process_eval(eval_at)
        result=await inspect_new_v40_stage2(
            self.db.pool,eval_at=eval_at,observed_at=datetime.now(UTC),
        )
        LOGGER.info(
            "V40 SHADOW status=%s stage2=%d vol30_skip=%d "
            "vol30_pass=%d live_authorization=false entry_allowed=false",
            result.status,result.validated_stage2,result.vol30_skip,
            result.vol30_passed_research_only,
        )
        # Verifiable market-signal provenance for supervised pilot operations.
        # Do NOT infer an order can be sent from this scanner receipt.
        for receipt in result.passed_signals:
            LOGGER.info(
                "V40 VERIFIED_SIGNAL signal_id=%d symbol=%s tier=%s "
                "p2_at=%s pre24_vol_pct=%.4f "
                "entry_allowed=false live_authorization=false no_order=true",
                receipt.signal_id,receipt.symbol,receipt.tier,
                receipt.p2_at.isoformat(),receipt.pre24_vol_pct,
            )


async def main() -> None:
    require_v40_shadow_render_guards(os.environ)
    configure_logging(os.getenv("LOG_LEVEL","INFO"))
    worker=V40Stage2RenderScanner()
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):
        loop.add_signal_handler(sig,worker.stop_event.set)
    await worker.run()


if __name__=="__main__":
    asyncio.run(main())

"""Real V40 Stage2 + subaccount integration probe, strictly NO ORDERS.

This is the missing Render-side SIGNAL TRANSPORT CHECK:
  dedicated read-only signal-role PostgreSQL -> immutable Stage2 ID ->
  49 causal Min30 bars -> VOL30 < 30 -> isolated subaccount signed GET
  -> V40 frozen model economics.
It is *not* a V40 live-order/trailing manager. Results are non-authorizing.

Run --once or --watch only under the isolated V40 trader's paper/kill
environment and an independent SELECT-only database credential
T100_V40_SIGNAL_READER_DSN, never the scanner's DATABASE_URL.

No SQL mutation, MEXC POST, stop changes, forced BTC trades or order retries.
Exceptions are redacted; no DSNs, API keys or raw venue messages are printed.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from typing import Any, Awaitable, Callable, Mapping, Protocol

from app.t100_live_v40_stage2_source import (
    PostgresV40Stage2Source, V40SourceBatch, V40SignalIntakeHalted,
)
from app.t100_v40_admission import VOL_SKIP_THRESHOLD, admission
from app.t100_v40_render_executor_disabled_worker import require_disabled_worker
from scripts.t100_v40_pilot_get_only import PilotGetOnlyResult, observe_live_pilot


@dataclass(frozen=True)
class V40SignalIntakeReport:
    status: str
    fresh_source_verified: bool = False
    signal_count: int = 0
    signal_id: int | None = None
    symbol: str | None = None
    tier: str | None = None
    pre24_vol_pct: float | None = None
    current_flat_account_verified: bool = False
    account_equity_usdt: str | None = None
    theoretical_v40_notional_usdt: str | None = None
    # Explicit release boundaries: neither a valid signal nor a funding
    # balance proves actual lot depth/CROSS/SL75 or a viable order.
    exact_lot_and_cross_risk_verified: bool = False
    native_stop_execution_verified: bool = False
    durable_entry_claim_verified: bool = False
    native_trailing_exit_verified: bool = False
    independent_nonflat_watchdog_verified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_posts: int = 0
    database_writes: int = 0


class Stage2Source(Protocol):
    async def latest_candidates(self, *, observed_at: datetime) -> V40SourceBatch: ...


def eligible_environment(env: Mapping[str, str]) -> bool:
    """Keep this probe restricted to the disabled, isolated trader."""
    return (
        require_disabled_worker(env)
        and env.get("T100_V40_EXECUTOR_KEY_PREFLIGHT_ENABLED")=="true"
        and env.get("T100_V40_ACCOUNT_PROFILE")=="DEDICATED_WEB_PILOT"
        and bool(env.get("T100_V40_SIGNAL_READER_DSN"))
        and not env.get("DATABASE_URL")
        and not env.get("T100_LIVE_DATABASE_URL")
    )


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        if type(value) is bool: return None
        d=Decimal(str(value))
        return d if d.is_finite() and d>0 else None
    except (ValueError, TypeError, InvalidOperation):
        return None


async def inspect_v40_signal_intake(
    env: Mapping[str,str], *,
    source: Stage2Source,
    observed_at: datetime,
    observe: Callable[..., Awaitable[PilotGetOnlyResult]] = observe_live_pilot,
) -> V40SignalIntakeReport:
    def fail(status: str) -> V40SignalIntakeReport:
        return V40SignalIntakeReport(status=status)
    if not eligible_environment(env):
        return fail("V40_SIGNAL_INTAKE_PAPER_KILL_OR_READER_DSN_REQUIRED")
    if not (
        type(observed_at) is datetime
        and observed_at.tzinfo is not None
        and observed_at.utcoffset() is not None
    ):
        return fail("V40_SIGNAL_INTAKE_CLOCK_INVALID")
    try:
        tape=await source.latest_candidates(observed_at=observed_at)
    except Exception:
        return fail("V40_SIGNAL_INTAKE_SOURCE_READ_ONLY_HALT")
    if (
        type(tape) is not V40SourceBatch
        or tape.status != "FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING"
        or tape.read_only_repeatable_read is not True
        or tape.history_complete is not True
        or tape.entry_allowed is not False or tape.live_authorization is not False
    ):
        return fail("V40_SIGNAL_INTAKE_SOURCE_SHAPE_HALT")
    if not tape.candidates:
        return V40SignalIntakeReport(
            status="V40_SIGNAL_INTAKE_FRESH_EMPTY_SOURCE_OBSERVED",
            fresh_source_verified=True,
        )
    if len(tape.candidates)!=1:
        return V40SignalIntakeReport(
            status="V40_SIGNAL_INTAKE_MULTIPLE_CANDIDATES_SUPERVISED_REVIEW",
            fresh_source_verified=True,signal_count=len(tape.candidates),
        )
    candidate=tape.candidates[0]
    if (
        type(candidate.signal_id) is not int or candidate.signal_id<=0
        or candidate.entry_allowed is not False
        or candidate.live_authorization is not False
        or not 0<=candidate.observed_age_seconds<=900
        or candidate.pre24_vol_pct<0
    ):
        return fail("V40_SIGNAL_INTAKE_CANDIDATE_PROVENANCE_HALT")
    common=dict(
        fresh_source_verified=True,signal_count=1,
        signal_id=candidate.signal_id,symbol=candidate.symbol,
        tier=candidate.tier,pre24_vol_pct=candidate.pre24_vol_pct,
    )
    if candidate.pre24_vol_pct>=VOL_SKIP_THRESHOLD:
        return V40SignalIntakeReport(
            status="V40_SIGNAL_INTAKE_VOL30_SKIP_REAL_SOURCE",**common,
        )
    key,secret=env.get("MEXC_API_KEY",""),env.get("MEXC_API_SECRET","")
    if (
        type(key) is not str or len(key)<12 or key!=key.strip()
        or type(secret) is not str or len(secret)<16 or secret!=secret.strip()
    ):
        return V40SignalIntakeReport(
            status="V40_SIGNAL_INTAKE_ACCOUNT_KEYS_MISSING",**common,
        )
    try:
        flat=await asyncio.wait_for(observe(key=key,secret=secret),timeout=30)
    except Exception:
        return V40SignalIntakeReport(
            status="V40_SIGNAL_INTAKE_SIGNED_ACCOUNT_GET_FAILED",**common,
        )
    if not (
        type(flat) is PilotGetOnlyResult
        and flat.status=="PILOT_FLAT_HEDGE_SIGNED_GET_BOOKENDS_NONAUTHORIZING"
        and flat.samples==2 and flat.position_count==0
        and flat.native_stop_count==0 and flat.hedge_mode_observed is True
        and flat.inventory_bookends_equal is True
        and flat.mexc_posts==0 and flat.db_mutations==0
        and flat.live_authorization is False
    ):
        return V40SignalIntakeReport(
            status="V40_SIGNAL_INTAKE_NONFLAT_OR_UNVERIFIED_NO_ENTRY",**common,
        )
    equity=_positive_decimal(flat.account_equity_usdt)
    free=_positive_decimal(flat.available_balance_usdt)
    if equity is None or free is None or free>equity+Decimal("0.00000001"):
        return V40SignalIntakeReport(
            status="V40_SIGNAL_INTAKE_EQUITY_INVALID",**common,
        )
    try:
        decision=admission(
            prior_bars=candidate.prior_bars,
            admission_open_ts=candidate.admission_open_ts,
            equity_usdt=float(min(equity,free)),
            marked_existing_usdt=0.0,
            stage1_atr7_pct=candidate.stage1_atr7_pct,
            active_positions=0,already_open_symbol=False,
            cost_bps=100.,
        )
        if decision.action!="ADMIT":
            return V40SignalIntakeReport(
                status="V40_SIGNAL_INTAKE_FROZEN_ADMISSION_"+decision.action,
                current_flat_account_verified=True,
                account_equity_usdt=str(equity),**common,
            )
        modeled=_positive_decimal(decision.modeled_notional_usdt)
        if modeled is None:
            return V40SignalIntakeReport(
                status="V40_SIGNAL_INTAKE_MODELED_SIZE_INVALID",**common,
            )
        return V40SignalIntakeReport(
            status="V40_GENUINE_SIGNAL_AND_FLAT_ACCOUNT_MODELED_NONAUTHORIZING",
            current_flat_account_verified=True,
            account_equity_usdt=str(equity),
            theoretical_v40_notional_usdt=str(min(
                modeled,Decimal("12.50"),min(equity,free)*Decimal("0.25"),
            )),
            **common,
        )
    except Exception:
        return V40SignalIntakeReport(
            status="V40_SIGNAL_INTAKE_FROZEN_ADMISSION_FAILED",**common,
        )


async def _once(env: Mapping[str,str]) -> V40SignalIntakeReport:
    if not eligible_environment(env):
        return V40SignalIntakeReport("V40_SIGNAL_INTAKE_PAPER_KILL_OR_READER_DSN_REQUIRED")
    import asyncpg
    # Explicit single read-only pool; every source query additionally checks
    # current_user=t100_v40_signal_reader and REPEATABLE READ/READ ONLY.
    try:
        pool=await asyncpg.create_pool(
            dsn=env["T100_V40_SIGNAL_READER_DSN"],
            ssl="require",min_size=1,max_size=1,timeout=12,
            command_timeout=15,
            server_settings={"default_transaction_read_only":"on"},
        )
    except Exception:
        return V40SignalIntakeReport("V40_SIGNAL_INTAKE_DB_CONNECTION_FAILED")
    try:
        source=PostgresV40Stage2Source(pool,restricted_source_reader=True)
        return await inspect_v40_signal_intake(
            env,source=source,observed_at=datetime.now(timezone.utc),
        )
    finally:
        await pool.close()


def main()->int:
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once",action="store_true")
    group.add_argument("--watch",action="store_true",
                       help="Observe real signals every 60 seconds; NO ORDERS")
    args=parser.parse_args()
    env=dict(os.environ)
    if not eligible_environment(env):
        print(json.dumps(asdict(V40SignalIntakeReport(
            "V40_SIGNAL_INTAKE_PAPER_KILL_OR_READER_DSN_REQUIRED"
        )),sort_keys=True))
        return 2
    if args.once:
        result=asyncio.run(_once(env))
        print(json.dumps(asdict(result),sort_keys=True))
        return 0 if result.fresh_source_verified else 2
    seen:set[int]=set()
    try:
        while True:
            result=asyncio.run(_once(env))
            if result.signal_id is None or result.signal_id not in seen:
                print(json.dumps(asdict(result),sort_keys=True),flush=True)
                if result.signal_id is not None:
                    seen.add(result.signal_id)
            if not result.fresh_source_verified:
                return 2
            asyncio.run(asyncio.sleep(60))
    except KeyboardInterrupt:
        return 0


if __name__=="__main__":
    raise SystemExit(main())

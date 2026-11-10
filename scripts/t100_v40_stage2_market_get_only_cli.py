"""One-command, READ ONLY fresh V40 Stage2 candidate + MEXC settings/depth audit.

This is a manual Mac-local research diagnostic for the user's dedicated
T100 WEB pilot, NOT the V40 live executor. It reads up to 32 recent,
validated, *causal* V40 Stage2 candidates from the existing restricted
PostgreSQL role, and quotes up to 4 newest candidates using the existing
MEXC signed-GET-only / public-GET candidate-market witness.

Never places a trade, edits leverage/stops, writes a DB row, deploys
Render, accesses BOXWAVE, opens BTC/ETH V40 entries, or changes account
permissions. The actual market witness requires per-symbol SHORT CROSS
1x and two stable signed risk-limit/leverage GETs as well as account
bookends and executable contract metadata; it does NOT grant any trade
permission, collateral certification, or autonomous restart certification.

The existing scanner-owner PostgreSQL External URL is requested via
ONE hidden terminal prompt; there is no separate runtime-role password.
The scanner-owner role must be mexc_scanner, and the connection defaults
to READ ONLY; every source query is also confined to an explicit
REPEATABLE READ, READ ONLY transaction. A high-privilege owner URL is
acceptable ONLY in this manual, nonauthorizing research diagnostic:
never reuse it for the live executor/worker. The dedicated executor
role intentionally lacks access to scanner Stage2 tables.

MEXC keys are read from Mac Keychain under the exact WEB pilot service,
never from shell environment. No DB or exchange credentials are saved
or printed. No monitoring witness/sentinel DB roles are reused.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import ssl
import sys
import time
from dataclasses import asdict,dataclass
from datetime import datetime,timezone
from pathlib import Path
from typing import Any,Mapping

import httpx

from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted,require_readonly_controls,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_v40_candidate_market_witness import (
    MexcV40CandidateMarketWitness,V40CandidateMarketHalted,
)
from app.t100_live_v40_stage2_source import (
    PostgresV40Stage2Source,V40SignalIntakeHalted,V40SourceBatch,
    V40SourceCandidate,MANUAL_SCANNER_READONLY_OWNER_ROLE,
)
from scripts.t100_v40_pilot_get_only import (
    BASE,KEY_ACCOUNT,SECRET_ACCOUNT,KeychainSignedGetOnly,_from_keychain,
)

WEB_PILOT_KEYCHAIN_SERVICE="t100225-mexc-web-pilot-subaccount-v1"
MAX_SOURCE=32
MAX_CHECKS=4
# Stable, source-owned failure codes only: never surface arbitrary
# PostgreSQL/driver errors, raw DSNs or untrusted exception text.
_ALLOWED_SOURCE_FAILURES=frozenset({
    "SOURCE_RESTRICTED_REPEATABLE_READ_CONTEXT_REQUIRED",
    "SOURCE_SIGNAL_QUERY_SHAPE_INVALID",
    "SOURCE_SIGNAL_BATCH_TRUNCATION_BLOCKED",
    "SOURCE_SIGNAL_IDENTITY_INVALID",
    "SOURCE_HISTORY_QUERY_SHAPE_INVALID",
    "SOURCE_DUPLICATE_SIGNAL_OR_SYMBOL_TIME",
    "SOURCE_READ_ONLY_POSTGRES_QUERY_FAILED",
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
    "SOURCE_NAIVE_TIMESTAMP",
    "SOURCE_SUBSECOND_CANDLE_CLOCK_INVALID",
    "SOURCE_INVALID_NUMBER",
    "SOURCE_INTAKE_ARGS_INVALID",
})
_ALLOWED_FAILURES=frozenset({
    "MARKET_SIGNED_GET_FAILED",
    "MARKET_PUBLIC_GET_FAILED",
    "MARKET_PUBLIC_ENVELOPE_INVALID",
    "MARKET_NOT_FLAT_HEDGE_ACCOUNT",
    "MARKET_ACCOUNT_BOOKEND_DRIFT",
    "MARKET_ASSET_BOOKEND_DRIFT",
    "MARKET_FLAT_ASSET_ECONOMICS_UNVERIFIED",
    "MARKET_USDT_COLLATERAL_DRIFT",
    "MARKET_COLLATERAL_COMPONENT_MISSING",
    "MARKET_COLLATERAL_COMPONENT_DRIFT",
    "MARKET_COUNTRY_CONTRACT_NOT_ELIGIBLE",
    "MARKET_CONTRACT_SIZE_GRID_INVALID",
    "MARKET_CONTRACT_LOT_GRID_INVALID",
    "MARKET_PRIVATE_RISK_SCOPE_INVALID",
    "MARKET_PRIVATE_SHORT_RISK_UNVERIFIED",
    "MARKET_PRIVATE_RISK_LEVEL_INVALID",
    "MARKET_PRIVATE_SHORT_SETTINGS_UNVERIFIED",
    "MARKET_PRIVATE_CROSS_ONE_X_UNVERIFIED",
    "MARKET_PRIVATE_RISK_LEVERAGE_CONFLICT",
    "MARKET_PRIVATE_CONFIG_BOOKEND_DRIFT",
    "MARKET_PRIVATE_CONFIG_SHAPE_INVALID",
    "MARKET_PRIVATE_VOLUME_BELOW_MINIMUM",
    "MARKET_BOOK_TIMESTAMP_OR_VERSION_STALE",
    "MARKET_CROSSED_ORDER_BOOK",
    "MARKET_DEPTH_LEVELS_INVALID",
    "MARKET_DEPTH_ROW_INVALID",
    "MARKET_DEPTH_NOT_STRICTLY_SORTED",
    "MARKET_TOTAL_CYCLE_STALE",
    "MARKET_CLOCK_INVALID",
})


@dataclass(frozen=True)
class CandidatePreview:
    symbol:str
    tier:str
    status:str
    short_cross_1x_bookended:bool=False
    public_contract_eligible:bool=False
    fresh_depth_observed:bool=False
    private_order_write_permission_verified:bool=False
    entry_allowed:bool=False


@dataclass(frozen=True)
class Stage2Preview:
    status:str
    fresh_validated_signals:int=0
    checked_symbols:int=0
    limited_for_research:bool=False
    candidates:tuple[CandidatePreview,...]=()
    independent_risk_and_collateral_certified:bool=False
    actual_order_execution_certified:bool=False
    independent_autonomous_restart_certified:bool=False
    entry_allowed:bool=False
    live_authorization:bool=False
    mexc_posts:int=0
    database_writes:int=0
    read_only:bool=True


def _halt(status:str)->Stage2Preview:
    return Stage2Preview(status=status)


async def preview_fresh_stage2(
    *,source:PostgresV40Stage2Source,
    private:ReadOnlyMexcProbe,public:Any,
    env:Mapping[str,str],
    clock_ms:Any,
    observed_at:datetime,
    max_checks:int=MAX_CHECKS,
)->Stage2Preview:
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return _halt("STAGE2_MARKET_PAPER_KILL_REQUIRED_HALT")
    if (
        type(source) is not PostgresV40Stage2Source
        or type(private) is not ReadOnlyMexcProbe
        or public is None or not callable(clock_ms)
        or type(observed_at) is not datetime
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
        or type(max_checks) is not int
        or not 1<=max_checks<=MAX_CHECKS
    ):
        return _halt("STAGE2_MARKET_INPUT_INVALID_HALT")
    try:
        batch=await source.latest_candidates(
            observed_at=observed_at,limit=MAX_SOURCE,
        )
    except V40SignalIntakeHalted as exc:
        safe=str(exc)
        return _halt(
            "STAGE2_MARKET_"+safe+"_HALT"
            if safe in _ALLOWED_SOURCE_FAILURES
            else "STAGE2_MARKET_READONLY_SOURCE_UNVERIFIED_HALT"
        )
    except Exception:
        return _halt("STAGE2_MARKET_READONLY_SOURCE_UNVERIFIED_HALT")
    if (
        type(batch) is not V40SourceBatch
        or batch.status!="FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING"
        or batch.read_only_repeatable_read is not True
        or batch.history_complete is not True
        or batch.live_authorization is not False
        or batch.entry_allowed is not False
        or type(batch.candidates) is not tuple
        or len(batch.candidates)>MAX_SOURCE
        or any(type(c) is not V40SourceCandidate
               for c in batch.candidates)
    ):
        return _halt("STAGE2_MARKET_SOURCE_CONTRACT_INVALID_HALT")
    if not batch.candidates:
        return Stage2Preview(status="STAGE2_NO_FRESH_ELIGIBLE_CANDIDATES_NONAUTHORIZING")
    if len({c.signal_id for c in batch.candidates})!=len(batch.candidates):
        return _halt("STAGE2_MARKET_DUPLICATE_SIGNAL_SOURCE_HALT")
    # Never claim all candidates inspected if the bounded research sweep
    # did not inspect every one. Newest first, no fake signal generation.
    selected=sorted(
        batch.candidates,
        key=lambda c:(c.admission_open_ts,c.signal_id),reverse=True,
    )[:max_checks]
    witness=MexcV40CandidateMarketWitness(
        private=private,public=public,clock_ms=clock_ms,
    )
    previews=[]
    for signal in selected:
        if (
            type(signal.symbol) is not str
            or re.fullmatch(r"[A-Z0-9]+_USDT",signal.symbol) is None
            or signal.symbol in ("BTC_USDT","ETH_USDT")
            or type(signal.tier) is not str
            or signal.tier not in ("HIGH_225","LOW_100")
        ):
            return _halt("STAGE2_MARKET_SIGNAL_IDENTITY_UNVERIFIED_HALT")
        try:
            quote=await witness.observe(signal)
        except V40CandidateMarketHalted as exc:
            safe=str(exc)
            previews.append(CandidatePreview(
                symbol=signal.symbol,tier=signal.tier,
                status=safe if safe in _ALLOWED_FAILURES else
                       "STAGE2_MARKET_CANDIDATE_GET_UNVERIFIED_HALT",
            ))
            continue
        except Exception:
            previews.append(CandidatePreview(
                symbol=signal.symbol,tier=signal.tier,
                status="STAGE2_MARKET_CANDIDATE_GET_UNVERIFIED_HALT",
            ))
            continue
        if not (
            quote.status=="V40_GET_ONLY_FLAT_ACCOUNT_AND_MARKET_QUOTED_NONAUTHORIZING"
            and quote.bookended_flat_account is True
            and quote.hedge_mode_observed is True
            and quote.private_risk_row_observed is True
            and quote.private_short_cross_1x_settings_bookended is True
            and quote.candidate_contract.market_state_and_country_verified is True
            and quote.candidate_contract.private_order_permission_verified is False
            and quote.private_key_order_permission_verified is False
            and quote.live_authorization is False
            and quote.entry_allowed is False
        ):
            previews.append(CandidatePreview(
                symbol=signal.symbol,tier=signal.tier,
                status="STAGE2_MARKET_CANDIDATE_INVARIANT_FAILED_HALT",
            ))
            continue
        previews.append(CandidatePreview(
            symbol=signal.symbol,tier=signal.tier,
            status="STAGE2_MARKET_CANDIDATE_GET_QUOTED_NONAUTHORIZING",
            short_cross_1x_bookended=True,
            public_contract_eligible=True,
            fresh_depth_observed=True,
        ))
    return Stage2Preview(
        status="STAGE2_MARKET_BOUNDED_GET_SWEEP_NONAUTHORIZING",
        fresh_validated_signals=len(batch.candidates),
        checked_symbols=len(previews),
        limited_for_research=len(batch.candidates)>max_checks,
        candidates=tuple(previews),
    )


async def _main(args:argparse.Namespace,env:Mapping[str,str])->Stage2Preview:
    if sys.platform!="darwin":
        return _halt("STAGE2_MARKET_MAC_ONLY_HALT")
    if args.keychain_service!=WEB_PILOT_KEYCHAIN_SERVICE:
        return _halt("STAGE2_MARKET_PILOT_SCOPE_INVALID_HALT")
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return _halt("STAGE2_MARKET_PAPER_KILL_REQUIRED_HALT")
    if type(args.max_checks) is not int or not 1<=args.max_checks<=MAX_CHECKS:
        return _halt("STAGE2_MARKET_BATCH_SIZE_INVALID_HALT")

    try:
        key=_from_keychain(args.keychain_service,KEY_ACCOUNT)
        secret=_from_keychain(args.keychain_service,SECRET_ACCOUNT)
    except Exception:
        return _halt("STAGE2_MARKET_WEB_PILOT_KEYCHAIN_UNAVAILABLE_HALT")
    # The existing scanner-owner URL includes its own DB login and
    # password; do not ask for, guess, rotate or substitute the unrelated
    # executor or witness/sentinel Keychain secrets.
    dsn=getpass.getpass(
        "Existing scanner-owner Render PostgreSQL External URL (hidden): "
    )
    if not dsn or not dsn.startswith(("postgresql://","postgres://")):
        return _halt("STAGE2_MARKET_SCANNER_OWNER_URL_REQUIRED_HALT")
    import asyncpg
    pool=None
    signed=None
    try:
        pool=await asyncpg.create_pool(
            dsn=dsn,
            ssl=ssl.create_default_context(),timeout=10,command_timeout=12,
            min_size=1,max_size=1,
            server_settings={"default_transaction_read_only":"on"},
        )
        signed=KeychainSignedGetOnly(key=key,secret=secret)
        async with httpx.AsyncClient(
            base_url=BASE,trust_env=False,
            follow_redirects=False,timeout=10,
        ) as public:
            return await asyncio.wait_for(preview_fresh_stage2(
                source=PostgresV40Stage2Source(
                    pool,manual_scanner_owner_readonly=True,
                ),
                private=ReadOnlyMexcProbe(signed),
                public=public,env=env,
                clock_ms=lambda:int(time.time()*1000),
                observed_at=datetime.now(timezone.utc),
                max_checks=args.max_checks,
            ),timeout=100)
    except Exception:
        return _halt("STAGE2_MARKET_GET_OR_RESTRICTED_DB_FAILED_HALT")
    finally:
        if signed is not None:
            await signed.close()
        if pool is not None:
            await pool.close()


def main()->None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keychain-service",required=True)
    parser.add_argument("--max-checks",type=int,default=MAX_CHECKS)
    parser.add_argument(
        "--output",default="~/Downloads/t100_v40_stage2_market_get_only.json",
    )
    args=parser.parse_args()
    try:
        report=asyncio.run(_main(args,dict(os.environ)))
    except Exception:
        report=_halt("STAGE2_MARKET_DIAGNOSTIC_FAILED_HALT")
    path=Path(args.output).expanduser()
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise SystemExit("STAGE2_MARKET_OUTPUT_PATH_UNSAFE")
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(asdict(report),sort_keys=True,indent=2)+"\n")
    path.chmod(0o600)
    print("MEXC_GET_ONLY=true DB_SELECT_ONLY=true POST_COUNT=0 DB_WRITES=0")
    print("REPORT_SAVED="+str(path))
    print(json.dumps(asdict(report),sort_keys=True))


if __name__=="__main__":
    main()

"""Manual Mac-only, scanner-owner SELECT-only historical V40 signal replay.

Use when the 15-minute live Stage2 window is legitimately empty. Replay up
to eight real eligible scanner signals from the past seven days and validate
their 49 completed predecessor candles; older signals are never tradable.
This is deliberately separate from live-intake, pilot Market GET and
the autonomous V40 executor.

Requires only an existing Render scanner-owner External PostgreSQL URL
at a single hidden prompt, plus explicit paper/kill controls. No separate
executor password, MEXC Keychain access, trade order, market API request,
DB write, account change, Render deploy, or BOXWAVE access.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import ssl
import sys
from dataclasses import asdict
from datetime import datetime,timezone
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted,require_readonly_controls,
)
from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_v40_stage2_source import (
    MANUAL_SCANNER_READONLY_OWNER_ROLE,PostgresV40Stage2Source,
)
from app.t100_live_v40_historical_stage2_audit import (
    HistoricalStage2Audit,inspect_historical_stage2,
    DEFAULT_LOOKBACK_DAYS,MAX_LOOKBACK_DAYS,MAX_HISTORICAL_RECORDS,
)


def _fail(code:str)->HistoricalStage2Audit:
    return HistoricalStage2Audit(status=code)


def _url_identity_only(dsn:str)->bool:
    """Parse locally and discard, never expose a connection URL in logs."""
    if type(dsn) is not str or len(dsn)>4096:
        return False
    try:
        url=urlsplit(dsn)
    except ValueError:
        return False
    return (
        url.scheme in ("postgresql","postgres")
        and url.username==MANUAL_SCANNER_READONLY_OWNER_ROLE
        and url.password is not None
        and url.path=="/"+EXPECTED_DATABASE
        and url.hostname is not None
    )


async def _main(args:argparse.Namespace,env:Mapping[str,str])->HistoricalStage2Audit:
    if sys.platform!="darwin":
        return _fail("V40_HISTORICAL_MAC_ONLY_HALT")
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return _fail("V40_HISTORICAL_PAPER_KILL_REQUIRED_HALT")
    if (
        type(args.max_records) is not int
        or not 1<=args.max_records<=MAX_HISTORICAL_RECORDS
        or type(args.lookback_days) is not int
        or not 1<=args.lookback_days<=MAX_LOOKBACK_DAYS
    ):
        return _fail("V40_HISTORICAL_LIMITS_INVALID_HALT")
    url=getpass.getpass(
        "Existing scanner-owner Render PostgreSQL External URL (hidden): "
    )
    if not _url_identity_only(url):
        return _fail("V40_HISTORICAL_OWNER_DB_URL_SCOPE_INVALID_HALT")

    import asyncpg
    pool=None
    try:
        pool=await asyncpg.create_pool(
            dsn=url,ssl=ssl.create_default_context(),
            timeout=10,command_timeout=12,
            min_size=1,max_size=1,
            server_settings={"default_transaction_read_only":"on"},
        )
        source=PostgresV40Stage2Source(
            pool,manual_scanner_owner_readonly=True,
        )
        return await asyncio.wait_for(inspect_historical_stage2(
            source,observed_at=datetime.now(timezone.utc),
            max_records=args.max_records,
            lookback_days=args.lookback_days,
        ),timeout=60)
    except Exception:
        return _fail("V40_HISTORICAL_DB_CONNECTION_FAILED_HALT")
    finally:
        if pool is not None:
            try:
                await pool.close()
            except Exception:
                pass


def main()->None:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--max-records",type=int,default=MAX_HISTORICAL_RECORDS)
    p.add_argument("--lookback-days",type=int,default=DEFAULT_LOOKBACK_DAYS)
    p.add_argument(
        "--output",default="~/Downloads/t100_v40_historical_stage2_audit.json",
    )
    args=p.parse_args()
    try:
        report=asyncio.run(_main(args,dict(os.environ)))
    except Exception:
        report=_fail("V40_HISTORICAL_RESEARCH_FAILED_HALT")
    path=Path(args.output).expanduser()
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise SystemExit("V40_HISTORICAL_OUTPUT_PATH_UNSAFE")
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(
        json.dumps(asdict(report),sort_keys=True,indent=2)+"\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    print("HISTORICAL_ONLY=true DB_SELECT_ONLY=true MEXC_POSTS=0 DB_WRITES=0")
    print("REPORT_SAVED="+str(path))
    print(json.dumps(asdict(report),sort_keys=True))


if __name__=="__main__":
    main()

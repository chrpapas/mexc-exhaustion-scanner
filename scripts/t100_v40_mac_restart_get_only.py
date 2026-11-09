"""Mac-local, *separate-process* T100 web-pilot restart witness.

Runs the existing fixed-Keychain, signed-GET-only FLAT account preflight in
TWO fresh Python processes. Each child creates and closes its own HTTP
session and performs two separate complete venue inventory reads. The
parent checks status, all safety flags, and account equity consistency.
This is useful current/flat connection survival evidence, NOT non-flat
checkpoint restoration, stop amendment atomicity, write permissions, or
authorization for a live T100/225 bot.

No MEXC POST, database access, Render change or BOXWAVE credentials.
The child process never receives secrets on argv/env; it reads the named
pilot Keychain service on the local Mac. Child stdout/stderr are never
forwarded to the shared report.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict,dataclass
from decimal import Decimal as D, InvalidOperation
from pathlib import Path
from typing import Any,Mapping

from app.t100_live_current_inventory_cli import WEB_PILOT_KEYCHAIN_SERVICE

GOOD="PILOT_FLAT_HEDGE_SIGNED_GET_BOOKENDS_NONAUTHORIZING"
READY="PILOT_TWO_PROCESS_FLAT_SIGNED_GET_RECONNECT_OBSERVED_NONAUTHORIZING"
MODULE="scripts.t100_v40_pilot_get_only"
SAFE_ENV={
    "T100_EXECUTION_MODE":"paper",
    "T100_LIVE_ORDER_API_ENABLED":"false",
    "T100_LIVE_KILL_SWITCH":"true",
}


@dataclass(frozen=True)
class PilotRestartReport:
    status: str
    credential_scope: str="T100_WEB_PILOT_SUBACCOUNT"
    separate_python_processes_observed: int=0
    signed_inventory_samples_observed: int=0
    flat_account_stable_across_processes: bool=False
    hedge_mode_verified: bool=False
    open_positions: int|None=None
    active_stops: int|None=None
    account_equity_usdt: str|None=None
    available_balance_usdt: str|None=None
    independent_nonflat_checkpoint_restart_verified: bool=False
    full_position_native_market_stop_verified_in_this_drill: bool=False
    continuous_stop_amendment_protection_verified: bool=False
    certified_api_trading_permission: bool=False
    independently_proven_subaccount_identity: bool=False
    exchange_atomicity_certified: bool=False
    live_authorization: bool=False
    entry_allowed: bool=False
    mexc_posts: int=0
    database_writes: int=0


def _fail(code: str) -> PilotRestartReport:
    return PilotRestartReport(status=code)


def _safe_decimal(raw: Any) -> D:
    if type(raw) is not str or not raw or len(raw)>40:
        raise ValueError("BAD_AMOUNT")
    try:
        number=D(raw)
    except (ValueError,InvalidOperation,TypeError):
        raise ValueError("BAD_AMOUNT") from None
    if not number.is_finite() or number<=0:
        raise ValueError("BAD_AMOUNT")
    return number


def assess_separate_processes(first: Any,second: Any) -> PilotRestartReport:
    """Strict redacted result policy; never accepts raw private GET data."""
    if type(first) is not dict or type(second) is not dict:
        return _fail("PILOT_RESTART_CHILD_REPORT_INVALID_HALT")
    accepted_keys={
        "status","account_equity_usdt","available_balance_usdt",
        "account_scope_independently_verified","cross_margin_collateral_verified",
        "db_mutations","exact_native_stop_lifecycle_verified",
        "hedge_mode_observed","inventory_bookends_equal","live_authorization",
        "mexc_posts","native_stop_count","position_count",
        "private_post_permission_verified","samples",
        "available_equity_roundoff_observed",
    }
    if set(first)!=accepted_keys or set(second)!=accepted_keys:
        return _fail("PILOT_RESTART_CHILD_REPORT_SCHEMA_HALT")
    for sample in (first,second):
        if sample.get("status")!=GOOD:
            return _fail("PILOT_RESTART_ONE_PROCESS_PREFLIGHT_FAILED_HALT")
        if not (
            sample.get("samples") == 2
            and type(sample.get("samples")) is int
            and sample.get("position_count") == 0
            and type(sample.get("position_count")) is int
            and sample.get("native_stop_count") == 0
            and type(sample.get("native_stop_count")) is int
            and sample.get("hedge_mode_observed") is True
            and sample.get("inventory_bookends_equal") is True
            and sample.get("account_scope_independently_verified") is False
            and sample.get("cross_margin_collateral_verified") is False
            and sample.get("exact_native_stop_lifecycle_verified") is False
            and sample.get("private_post_permission_verified") is False
            and sample.get("live_authorization") is False
            and type(sample.get("mexc_posts")) is int
            and sample["mexc_posts"]==0
            and type(sample.get("db_mutations")) is int
            and sample["db_mutations"]==0
            and type(sample.get("available_equity_roundoff_observed")) is bool
        ):
            return _fail("PILOT_RESTART_CHILD_SAFETY_INVARIANT_HALT")
    try:
        e1,e2=(_safe_decimal(s["account_equity_usdt"]) for s in (first,second))
        a1,a2=(_safe_decimal(s["available_balance_usdt"]) for s in (first,second))
        for e,a in ((e1,a1),(e2,a2)):
            if a-e>D("0.00000001"):
                return _fail("PILOT_RESTART_BALANCE_PRECISION_HALT")
    except ValueError:
        return _fail("PILOT_RESTART_CHILD_BALANCE_INVALID_HALT")
    if e1!=e2 or a1!=a2:
        return _fail("PILOT_RESTART_EQUITY_CHANGED_BETWEEN_PROCESSES_HALT")
    return PilotRestartReport(
        status=READY,
        separate_python_processes_observed=2,
        signed_inventory_samples_observed=4,
        flat_account_stable_across_processes=True,
        hedge_mode_verified=True,
        open_positions=0,active_stops=0,
        account_equity_usdt=str(e2),
        available_balance_usdt=str(a2),
    )


def run_child(service: str,output: Path,env: Mapping[str,str]) -> Any:
    """One fresh interpreter; no shell or credentials on argv/env."""
    try:
        child=subprocess.run(
            (
                sys.executable,"-m",MODULE,
                "--keychain-service",service,
                "--output",str(output),
            ),
            capture_output=True,check=False,timeout=60,
            env=dict(env),
        )
    except Exception:
        return None
    if child.returncode!=0:
        return None
    try:
        if output.is_symlink() or not output.is_file() or output.stat().st_size>8192:
            return None
        return json.loads(output.read_text(encoding="utf-8"))
    except (OSError,UnicodeError,ValueError,TypeError):
        return None


def run_mac_restart_drill(
    *, service: str,env: Mapping[str,str],
) -> PilotRestartReport:
    if (
        sys.platform!="darwin"
        or type(service) is not str
        or service!=WEB_PILOT_KEYCHAIN_SERVICE
        or type(env) is not dict
        or any(env.get(k)!=v for k,v in SAFE_ENV.items())
    ):
        return _fail("PILOT_RESTART_MAC_PAPER_KILL_OR_SCOPE_REQUIRED_HALT")
    # Strongly preserve the user's execution mode and kill switch, even
    # if another local process changed environment after validation.
    child_env=dict(env)
    child_env.update(SAFE_ENV)
    with tempfile.TemporaryDirectory(prefix="t100-v40-readonly-restart-") as tmp:
        first=run_child(service,Path(tmp)/"first.json",child_env)
        if first is None:
            return _fail("PILOT_RESTART_FIRST_PROCESS_FAILED_HALT")
        second=run_child(service,Path(tmp)/"second.json",child_env)
        if second is None:
            return _fail("PILOT_RESTART_SECOND_PROCESS_FAILED_HALT")
        return assess_separate_processes(first,second)


def main() -> None:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--keychain-service",required=True,
        help="The fixed T100 WEB pilot service (never an older pilot or BOXWAVE)",
    )
    p.add_argument(
        "--output",default="~/Downloads/t100_v40_mac_restart_get_only.json",
    )
    args=p.parse_args()
    result=run_mac_restart_drill(service=args.keychain_service,env=dict(os.environ))
    path=Path(args.output).expanduser()
    if path.is_symlink() or path.exists() and not path.is_file():
        raise SystemExit("PILOT_RESTART_OUTPUT_PATH_UNSAFE")
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(asdict(result),sort_keys=True,indent=2)+"\n")
    path.chmod(0o600)
    print("MEXC_GET_ONLY=true PROCESS_RESTART_DRILL=true POST_COUNT=0 DB_WRITES=0")
    print("REPORT_SAVED="+str(path))
    print(json.dumps(asdict(result),sort_keys=True))


if __name__=="__main__":
    main()

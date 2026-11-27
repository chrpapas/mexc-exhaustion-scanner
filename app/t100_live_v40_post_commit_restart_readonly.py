"""Manual-only T100 V40 protected nonflat restart GET/SELECT audit.

Orchestrates the existing restricted read-only four-slot ledger and account
inventory APIs with the strict V40 native MARKET stop readback. Does NOT
create a position, submit any order, change a stop, mutate SQL, or enable
the T100 live adapter. A flat account is a *refusal* to claim a nonflat
restart, not a reason to manufacture a test position.

The calling CLI must provide a strictly GET-only transport and read-only
REPEATABLE READ ledger connection. The user supplies the exact durable
checkpoint position ID; no arbitrary/manual position can be adopted.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any,Mapping

from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted,require_readonly_controls,
)
from app.t100_live_durable_recovery import checkpoint_from_row
from app.t100_live_four_slot_mexc_readonly import _components
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotCoordinator,_ledger_digest,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_v40_post_commit_restart_audit import (
    V40PostCommitRestartAuditor,
)


def _report(
    status:str,*,slots:int|None=None,
    native_market_sl_verified:bool=False,
) -> dict[str,Any]:
    return {
        "status":status,
        "recovered_owned_slots":slots if type(slots) is int and 1<=slots<=4 else None,
        "two_stable_nonflat_snapshot_rounds_verified":(
            status=="POST_COMMIT_NATIVE_MARKET_SL_GETS_STABLE_READONLY_NONAUTHORIZING"
        ),
        "native_market_sl_all_owned_slots_get_verified":(
            native_market_sl_verified is True
        ),
        "actual_process_restart_verified":False,
        "exchange_stop_atomicity_certified":False,
        "continuous_protection_during_amendment_certified":False,
        "private_post_permission_verified":False,
        "account_scope_independently_verified":False,
        "live_authorization":False,
        "entry_allowed":False,
        "mexc_posts":0,
        "database_writes":0,
        "read_only":True,
    }


async def run_readonly_v40_post_commit_restart_audit(
    *,ledger:Any,client:Any,env:Mapping[str,str],
    position_id:str,
) ->dict[str,Any]:
    try:
        require_readonly_controls(env)
    except CrossRiskDiagnosticHalted:
        return _report("POST_COMMIT_PAPER_KILL_REQUIRED_HALT")
    if (
        type(position_id) is not str
        or re.fullmatch(r"[1-9][0-9]*",position_id) is None
    ):
        return _report("POST_COMMIT_EXPECTED_POSITION_ID_INVALID_HALT")
    # Fail before starting any MEXC GET unless there is a typed durable
    # V40 trailing checkpoint at revision >= 2. The current flat web pilot
    # has none; this must NOT be silently upgraded to recovered nonflat.
    try:
        initial=await ledger.read_snapshot()
        _ledger_digest(initial)
        if type(initial.checkpoints) is not list:
            raise ValueError("POST_COMMIT_LEDGER_CHECKPOINTS_NOT_A_LIST")
        cps=[checkpoint_from_row(row) for row in initial.checkpoints]
    except Exception:
        return _report("POST_COMMIT_DURABLE_LEDGER_PRECHECK_FAILED_HALT")
    candidates=[cp for cp in cps if cp.position_id==position_id]
    if len(candidates)!=1:
        return _report("POST_COMMIT_EXPECTED_OWNED_CHECKPOINT_ABSENT_HALT")
    expected=candidates[0]
    if (
        expected.checkpoint_version<2
        or not expected.armed
        or expected.best_profit_pct<14
    ):
        return _report("POST_COMMIT_EXPECTED_TRAILING_REVISION_NOT_READY_HALT")
    try:
        venue,completeness=_components(client)
        auditor=V40PostCommitRestartAuditor(
            FourSlotSnapshotCoordinator(
                ledger=ledger,venue=venue,completeness=completeness,
            ),
            probe=ReadOnlyMexcProbe(client),
        )
        result=await asyncio.wait_for(
            auditor.observe(expected=expected),timeout=75,
        )
    except Exception:
        return _report("POST_COMMIT_REAL_READONLY_CONNECTION_FAILED_FREEZE")
    allowed="POST_COMMIT_NATIVE_MARKET_SL_GETS_STABLE_READONLY_NONAUTHORIZING"
    if result.status!=allowed:
        # Untrusted internal error status may not contain raw venue data,
        # but only fixed recognized freeze codes are allowed in output.
        safe={
            "POST_COMMIT_EXPECTED_CHECKPOINT_INVALID_FREEZE",
            "POST_COMMIT_READONLY_SNAPSHOT_FAILED_FREEZE",
            "POST_COMMIT_RESTART_PROVENANCE_UNVERIFIED_FREEZE",
            "POST_COMMIT_RESTART_POSITION_RECOVERY_INVALID_FREEZE",
            "POST_COMMIT_RESTART_SLOT_COUNT_INVALID_FREEZE",
            "POST_COMMIT_LEDGER_READBACK_CHANGED_FREEZE",
            "POST_COMMIT_LEDGER_READBACK_UNVERIFIED_FREEZE",
            "POST_COMMIT_EXPECTED_REVISION_NOT_DURABLE_FREEZE",
            "POST_COMMIT_EXPECTED_POSITION_NOT_RECOVERED_FREEZE",
            "POST_COMMIT_RECOVERED_CHECKPOINT_DRIFT_FREEZE",
            "POST_COMMIT_SIGNED_NATIVE_STOP_GET_UNVERIFIED_FREEZE",
            "POST_COMMIT_INDEPENDENT_SNAPSHOT_DRIFT_FREEZE",
            "POST_COMMIT_NATIVE_STOP_GET_BOOKENDS_DRIFT_FREEZE",
        }
        return _report(
            result.status if result.status in safe else
            "POST_COMMIT_RESTART_UNRECOGNIZED_PROVENANCE_FREEZE"
        )
    if not (
        result.expected_checkpoint_recovered is True
        and result.two_independent_full_account_snapshots_matched is True
        and result.native_market_stop_type_verified_after_restart is True
        and result.live_authorization is False
        and result.entry_allowed is False
        and result.exchange_posts==0
        and result.db_writes==0
    ):
        return _report("POST_COMMIT_RESTART_OUTPUT_INVARIANT_FAILED_FREEZE")
    return _report(
        allowed,slots=result.recovered_slots,
        native_market_sl_verified=True,
    )

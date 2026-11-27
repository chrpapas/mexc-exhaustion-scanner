"""Protected V40 watch: continue only with independent live owned guardian."""
import asyncio
import json

import pytest
from app.t100_v40_emergency_nonflat_gate import V40OwnedGuardianHealth
from app.t100_v40_emergency_runner import (
    V40EmergencyRunnerStatus,run_emergency_watch,
)
from tests.test_t100_v40_emergency_runner import env


def healthy():
    return V40OwnedGuardianHealth(
        "V40_ONE_OWNED_CROSS_NATIVE_MARKET_SL_INDEPENDENTLY_OBSERVED",
        owned_cross_native_sl_observed=True,
        fresh=True,incident_clear=True,
    )


def run(statuses,healths,cycles=5):
    close_status=list(statuses)
    guardian_status=list(healths)
    events=[]
    async def close(_):
        events.append("EXCHANGE_AND_SQL_GET_ONLY")
        return close_status.pop(0)
    async def guardian(_):
        events.append("INDEPENDENT_NONFLAT_GUARDIAN_DB")
        return guardian_status.pop(0)
    async def pause(t):
        assert t==5.
        events.append("PAUSE")
    rendered=[]
    code=asyncio.run(run_emergency_watch(
        env(),close_cycle=close,owned_guardian=guardian,
        pause=pause,emit=rendered.append,max_cycles=cycles,
    ))
    return code,events,[json.loads(x) for x in rendered]


def no_target():
    return V40EmergencyRunnerStatus(
        "V40_EMERGENCY_EXIT_NO_CERTIFIED_MISSING_STOP_NO_POST",
    )


def recovered():
    return V40EmergencyRunnerStatus(
        "V40_EMERGENCY_CLOSE_CHECKPOINT_INTENT_AND_SLOT_CLOSED_VERIFIED",
        claim_committed=True,exchange_close_post_may_have_occurred=True,
        signed_close_and_flat_verified=True,
        durable_checkpoint_and_slot_closed=True,
    )


def test_two_healthy_protected_checks_then_exact_close_can_finish():
    result,events,logs=run(
        [no_target(),no_target(),recovered()],
        [healthy(),healthy()],
    )
    assert result==0
    assert events==[
        "EXCHANGE_AND_SQL_GET_ONLY","INDEPENDENT_NONFLAT_GUARDIAN_DB",
        "PAUSE","EXCHANGE_AND_SQL_GET_ONLY",
        "INDEPENDENT_NONFLAT_GUARDIAN_DB","PAUSE",
        "EXCHANGE_AND_SQL_GET_ONLY",
    ]
    assert len(logs)==3
    assert logs[-1]["durable_checkpoint_and_slot_closed"] is True
    assert all(x["new_entries_allowed"] is False for x in logs)


@pytest.mark.parametrize("failure",[
    V40OwnedGuardianHealth("V40_OWNED_GUARDIAN_NOT_CURRENT_OR_LATCHED"),
    V40OwnedGuardianHealth("V40_OWNED_GUARDIAN_DB_UNAVAILABLE"),
    V40OwnedGuardianHealth(
        "V40_ONE_OWNED_CROSS_NATIVE_MARKET_SL_INDEPENDENTLY_OBSERVED",
        owned_cross_native_sl_observed=True,fresh=False,
        incident_clear=True,
    ),
])
def test_missing_live_guardian_fails_after_one_nonmutating_cycle(failure):
    result,events,logs=run([no_target()],[failure])
    assert result==2
    assert events==[
        "EXCHANGE_AND_SQL_GET_ONLY","INDEPENDENT_NONFLAT_GUARDIAN_DB",
    ]
    assert len(logs)==1 and not logs[0]["new_entries_allowed"]


def test_any_claim_or_close_unknown_never_creates_another_close_attempt():
    result,events,logs=run([
        V40EmergencyRunnerStatus(
            "V40_EMERGENCY_CLOSE_POST_ACK_AWAIT_SIGNED_FLAT_RECONCILIATION",
            claim_committed=True,exchange_close_post_may_have_occurred=True,
        )],[healthy()])
    assert result==2 and events==["EXCHANGE_AND_SQL_GET_ONLY"]
    assert logs[0]["claim_committed"] is True


def test_even_many_healthy_protected_cycles_is_not_a_permit():
    result,events,logs=run(
        [no_target(),no_target()],[healthy(),healthy()],cycles=2,
    )
    assert result==2
    assert len(logs)==2
    assert not any(x["new_entries_allowed"] or x["live_authorization"] for x in logs)


def test_forged_completed_status_without_signed_flat_cannot_be_success():
    result,events,logs=run([
        V40EmergencyRunnerStatus(
            "V40_EMERGENCY_CLOSE_CHECKPOINT_INTENT_AND_SLOT_CLOSED_VERIFIED",
            durable_checkpoint_and_slot_closed=True,
            signed_close_and_flat_verified=False,
        )],[])
    assert result==2 and len(events)==1

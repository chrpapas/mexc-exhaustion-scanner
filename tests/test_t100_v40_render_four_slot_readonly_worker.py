"""V40 real-source four-slot integration tests: GET/SELECT-only, no permissions."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import pytest

from app.t100_v40_render_four_slot_readonly_worker import (
    V40FourSlotWiringReport,
    V40ReadOnlyPortfolioPorts,
    validate_readonly_config,
    examine_wired_readonly,
    run_once,
    run_readonly_monitor,
)
from tests.test_t100_v40_live_portfolio_supervisor import flat, active
from app.t100_v40_guardian_post_gate import V40GuardianPostGateEvidence


def configuration():
    return {
        "T100_V40_FOUR_SLOT_READONLY_WIRING_ENABLED": "true",
        "T100_V40_EXECUTOR_DISABLED_WORKER": "true",
        "T100_EXECUTION_MODE": "paper",
        "TRADING_MODE": "paper",
        "T100_LIVE_KILL_SWITCH": "true",
        "T100_LIVE_ORDER_API_ENABLED": "false",
        "EXECUTION_ENABLED": "false",
        "T100_V40_ACCOUNT_PROFILE": "DEDICATED_WEB_PILOT",
        "T100_V40_SIGNAL_READER_DSN": "postgresql://reader:a@host/x",
        "T100_V40_EXECUTOR_RUNTIME_DSN": "postgresql://runtime:a@host/x",
        "MEXC_API_KEY": "a"*16,
        "MEXC_API_SECRET": "b"*32,
    }


def test_only_explicit_hard_paper_kill_and_separate_roles_allow_network_init():
    assert validate_readonly_config(configuration())
    for key,value in [
        ("TRADING_MODE","live"),
        ("T100_LIVE_ORDER_API_ENABLED","true"),
        ("EXECUTION_ENABLED","true"),
        ("T100_LIVE_KILL_SWITCH","false"),
        ("T100_V40_EXECUTOR_DISABLED_WORKER","false"),
        ("T100_V40_FIRST_CANARY_ORDER_WORKER_ENABLED","true"),
        ("T100_V40_FOUR_SLOT_READONLY_WIRING_ENABLED","false"),
        ("T100_V40_ACCOUNT_PROFILE","OTHER_SUBACCOUNT"),
    ]:
        bad=configuration()
        bad[key]=value
        assert not validate_readonly_config(bad),key
    bad=configuration()
    bad["T100_V40_EXECUTOR_RUNTIME_DSN"]=bad["T100_V40_SIGNAL_READER_DSN"]
    assert not validate_readonly_config(bad)
    bad=configuration()
    bad["MEXC_API_KEY"]=""
    assert not validate_readonly_config(bad)


@pytest.mark.asyncio
async def test_wrong_config_halts_before_connections():
    outcome=await run_once({"T100_V40_FOUR_SLOT_READONLY_WIRING_ENABLED":"true"})
    assert outcome.status=="V40_FOUR_SLOT_READONLY_CONFIG_HALT"
    assert outcome.live_authorization is False
    assert outcome.exchange_posts==outcome.database_writes==0


class FakeSource:
    def __init__(self, batch):
        self.batch=batch
        self.calls=0
    async def latest_candidates(self, *, observed_at):
        self.calls+=1
        assert observed_at.tzinfo is not None
        return self.batch


class FakeGate:
    def __init__(self, evidence):
        self.evidence=evidence
        self.calls=0
    async def verify(self):
        self.calls+=1
        return self.evidence


def mock_snapshot(monkeypatch, frame):
    from app import t100_live_four_slot_mexc_readonly as venue_mod
    from app import t100_live_four_slot_snapshot_coordinator as snap_mod
    monkeypatch.setattr(venue_mod, "_components", lambda _get: (object(),object()))
    class Coordinator:
        def __init__(self, *, ledger, venue, completeness):
            assert venue is not None and completeness is not None
        async def inspect(self):
            return frame.snapshot
    monkeypatch.setattr(snap_mod, "FourSlotSnapshotCoordinator", Coordinator)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_candidate,expected",[
    (False,"V40_SUPERVISOR_WAIT_GENUINE_STAGE2"),
    (True,"V40_SUPERVISOR_ENTRY_PERMIT_UNAVAILABLE"),
])
async def test_real_components_feed_supervisor_but_never_issue_permit(
    monkeypatch, with_candidate, expected,
):
    frame=await flat(with_candidate=with_candidate)
    mock_snapshot(monkeypatch,frame)
    source=FakeSource(frame.source)
    gate=FakeGate(frame.guardian)
    result=await examine_wired_readonly(
        source=source, ledger=object(), signed_get=object(),
        guardian=gate,nonflat_guardian=FakeGate(None),
        observed_at=datetime.now(timezone.utc),
    )
    assert result.status == expected
    assert result.observed_slots == 0
    assert result.candidate_count == (1 if with_candidate else 0)
    assert result.scanner_connected is True
    assert result.account_snapshot_connected is True
    assert result.guardian_connected is True
    assert result.complete_exit_runtime_connected is False
    assert result.exchange_posts==result.database_writes==0
    assert result.entry_allowed is False and result.live_authorization is False
    assert source.calls==1 and gate.calls==1


@pytest.mark.asyncio
async def test_nonflat_ledger_and_guardian_are_not_used_to_invent_stop_bar(monkeypatch):
    frame=active(2,with_candidate=True)
    mock_snapshot(monkeypatch,frame)
    source=FakeSource(frame.source)
    gate=FakeGate(frame.guardian)
    result=await examine_wired_readonly(
        source=source, ledger=object(), signed_get=object(),
        guardian=FakeGate(None), nonflat_guardian=gate,
        observed_at=datetime.now(timezone.utc),
    )
    assert result.status=="V40_FOUR_SLOT_NONFLAT_BAR_EXIT_RUNTIME_NOT_CONNECTED_HALT"
    assert result.observed_slots==2
    assert result.complete_exit_runtime_connected is False
    assert result.entry_allowed is False and result.live_authorization is False
    assert source.calls==0 and gate.calls==1
    assert result.exchange_posts==result.database_writes==0


@pytest.mark.asyncio
async def test_latced_guardian_blocks_real_signal(monkeypatch):
    frame=await flat(with_candidate=True)
    mock_snapshot(monkeypatch,frame)
    latched=V40GuardianPostGateEvidence("V40_GUARDIAN_POST_GATE_LATCH_OR_FRESHNESS_HALT")
    result=await examine_wired_readonly(
        source=FakeSource(frame.source),ledger=object(),signed_get=object(),
        guardian=FakeGate(latched),nonflat_guardian=FakeGate(None),
        observed_at=datetime.now(timezone.utc),
    )
    assert result.status=="V40_SUPERVISOR_GUARDIAN_ACCOUNT_OR_SOURCE_HALT"
    assert result.guardian_connected is False
    assert result.exchange_posts==0 and result.database_writes==0


@pytest.mark.asyncio
async def test_all_mutation_ports_raise_and_are_never_silent():
    ports=V40ReadOnlyPortfolioPorts(object())
    for action in [
        ports.enter_one(),
        ports.tighten_one_stop(),
        ports.reconcile_native_close(),
        ports.persist_unchanged_stop_checkpoint(),
    ]:
        with pytest.raises(RuntimeError, match="FORBIDDEN"):
            await action
    assert ports.mutation_calls==4


@pytest.mark.asyncio
async def test_repeating_monitor_rejects_unknown_halt_without_sleep(monkeypatch,capsys):
    from app import t100_v40_render_four_slot_readonly_worker as worker
    calls=0
    async def one(_env):
        nonlocal calls
        calls+=1
        return V40FourSlotWiringReport("V40_FOUR_SLOT_SNAPSHOT_UNVERIFIED_HALT")
    monkeypatch.setattr(worker,"run_once",one)
    code=await run_readonly_monitor(configuration(),max_cycles=100)
    assert code==2 and calls==1
    assert '"live_authorization": false' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_repeating_monitor_healthy_flat_still_never_trades(monkeypatch,capsys):
    from app import t100_v40_render_four_slot_readonly_worker as worker
    calls=0
    async def one(_env):
        nonlocal calls
        calls+=1
        return V40FourSlotWiringReport("V40_SUPERVISOR_WAIT_GENUINE_STAGE2")
    monkeypatch.setattr(worker,"run_once",one)
    code=await run_readonly_monitor(configuration(),max_cycles=1)
    assert code==0 and calls==1
    assert '"exchange_posts": 0' in capsys.readouterr().out

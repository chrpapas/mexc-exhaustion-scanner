"""Same MEXC signed account current-risk reader fault matrix; no network."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

import app.t100_v40_unreleased_same_signer_risk_reader as m
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from tests.test_t100_v40_unreleased_current_portfolio_risk import bundle


class Public:
    base_url="https://api.mexc.com"
    async def get(self,*args,**kwargs):
        raise AssertionError("unmocked public HTTP access")


def test_constructor_rejects_unpinned_public_host():
    with pytest.raises(m.V40SameSignerRiskHalted):
        m.V40SameSignerCurrentRiskReader(
            ledger=PostgresFourSlotLedgerReader(object()),
            private=ReadOnlyMexcProbe(object()),
            public=SimpleNamespace(base_url="http://localhost",
                                   get=lambda x:x),
            clock_ms=lambda:1002000,
        )


def run(monkeypatch, *,changes=None):
    kw=bundle(2)
    changed=changes or {}
    read=kw["risk_read"]
    observation=kw["risk_observation"]
    actual_probe=ReadOnlyMexcProbe(object())
    actual_public=Public()
    ledger=PostgresFourSlotLedgerReader(object())
    ledger_calls=[]
    async def read_snapshot():
        ledger_calls.append("select")
        return kw["ledger"]
    monkeypatch.setattr(ledger,"read_snapshot",read_snapshot)

    inventory_calls=[]
    class FakeSnapshot:
        def __init__(self,*,ledger,venue,completeness):
            assert ledger is actual_ledger
            assert venue.probe is actual_probe
            assert completeness.probe is actual_probe
            self.venue=venue
        async def inspect(self):
            inventory_calls.append("signed_get")
            if changed.get("post_inventory_drift") and len(inventory_calls)==2:
                return replace(kw["snapshot"],critical_inventory_hash="0"*64)
            if changed.get("flat_account"):
                return replace(kw["snapshot"],status="FLAT_OBSERVED_NONAUTHORIZING")
            return kw["snapshot"]
    actual_ledger=ledger

    market_calls=[]
    class FakeCollector:
        def __init__(self,*,private,public,clock_ms,max_frame_ms):
            assert private is actual_probe and public is actual_public
            market_calls.append("same_mexc_signer_verified")
        async def read(self):
            market_calls.append("cross_risk_read")
            return read
    async def observe_cross(reader,*,now_ms,max_age_ms,max_separation_ms):
        market_calls.append("cross_risk_two_read_window")
        await reader.read()
        await reader.read()
        if changed.get("risk_window_invalid"):
            return replace(observation,status="CROSS_RISK_CRITICAL_STATE_DRIFT")
        return observation

    monkeypatch.setattr(m,"FourSlotSnapshotCoordinator",FakeSnapshot)
    monkeypatch.setattr(m,"MexcReadOnlyCrossRiskCollector",FakeCollector)
    monkeypatch.setattr(m,"observe_cross_risk_window",observe_cross)
    tick=1002000+changed.get("elapsed_ms",0)
    client=m.V40SameSignerCurrentRiskReader(
        ledger=ledger,private=actual_probe,public=actual_public,
        clock_ms=lambda:tick,
    )
    result=asyncio.run(client.observe_current())
    return result,inventory_calls,ledger_calls,market_calls


def test_real_source_pipeline_same_get_signer_final_account_bookend(monkeypatch):
    result,inventory,ledger,market=run(monkeypatch)
    assert result.status=="V40_CURRENT_EXACT_FOUR_SLOT_FAIR_AND_TIER_RESEARCH_NONAUTHORIZING"
    assert result.slots==2
    assert result.all_owned_positions_and_native_stops_verified
    assert inventory==["signed_get","signed_get"]
    assert ledger==["select","select"]
    assert market==[
        "same_mexc_signer_verified",
        "cross_risk_two_read_window",
        "cross_risk_read","cross_risk_read",
    ]
    assert not result.entry_allowed and not result.live_authorization
    assert result.database_writes==result.mexc_posts==0


@pytest.mark.parametrize("change,reason",[
    ("post_inventory_drift","V40_SAME_SIGNER_POST_RISK_ACCOUNT_OR_LEDGER_DRIFT"),
    ("flat_account","V40_SAME_SIGNER_OWNED_ACCOUNT_NOT_PROVEN"),
    ("risk_window_invalid","V40_SAME_SIGNER_CURRENT_RISK_WINDOW_UNVERIFIED"),
    ("elapsed_ms","V40_SAME_SIGNER_POST_RISK_ACCOUNT_OR_LEDGER_DRIFT"),
])
def test_any_stale_cross_risk_changed_inventory_or_flat_halts(monkeypatch,change,reason):
    values={change:21000 if change=="elapsed_ms" else True}
    result,inventory,ledger,market=run(monkeypatch,changes=values)
    assert result.status==reason
    assert not result.same_venue_inventory_in_both_readers
    assert not result.entry_allowed and not result.live_authorization
    assert result.mexc_posts==result.database_writes==0

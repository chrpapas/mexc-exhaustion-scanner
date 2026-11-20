"""Live guardian 0–4 witness wiring stays GET-only, no order permit."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

import app.t100_v40_guardian_account_and_owned as guard
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_guardian_readonly_once import GetOnlyMexcTransport
from app.t100_v40_canary_account_guardian import V40CanaryGuardianHeartbeat
from app.t100_v40_canary_durable_guardian import (
    _status_for_outbox,_safe_observer_detail,
)
from tests.test_t100_v40_four_slot_signed_observer import inputs


async def observe(monkeypatch,n,*,drop_stop=False,foreign_position=False):
    ledger,venue,complete=inputs(n)
    if drop_stop and n:
        venue.first_stops[1 if n>1 else 0]["stopLossType"]=1
        venue.last_stops[1 if n>1 else 0]["stopLossType"]=1
    if foreign_position:
        for arr in (venue.first_positions,venue.last_positions):
            arr.append(dict(arr[0],positionId="999"))
    typed_ledger=PostgresFourSlotLedgerReader(object())
    async def proxy_snapshot(self):
        return await ledger.read_snapshot()
    monkeypatch.setattr(PostgresFourSlotLedgerReader,"read_snapshot",proxy_snapshot)
    class Probe:
        def __init__(self,client):pass
        async def raw_position_mode(self):return 1
    monkeypatch.setattr(guard,"ReadOnlyMexcProbe",Probe)
    monkeypatch.setattr(guard,"_components",lambda _: (venue,complete))
    get=object.__new__(GetOnlyMexcTransport)
    heartbeat=await guard.observe_v40_account_and_owned(
        ledger=typed_ledger,signed_get=get,
    )
    return heartbeat


@pytest.mark.asyncio
@pytest.mark.parametrize("n",[2,3,4])
async def test_real_guardian_admits_protected_two_to_four_without_trade_permit(
    monkeypatch,n,
):
    x=await observe(monkeypatch,n)
    assert x.status==(
        "V40_GUARDIAN_TWO_TO_FOUR_OWNED_CROSS_NATIVE_SL_OBSERVED_NONAUTHORIZING"
    )
    assert x.owned_cross_positions_count==n
    assert x.all_owned_cross_native_market_sl_verified
    assert x.ledger_provenance_verified
    assert x.inventory_two_signed_get_bookends
    assert x.hedge_mode_verified
    assert not x.single_owned_cross_native_sl_verified
    assert not x.monitored_account_flat and not x.incident_alert
    assert not x.live_authorization and not x.entry_allowed
    assert x.mexc_posts==x.database_writes==0
    assert _status_for_outbox(x)==(
        "OBSERVER_V40_CANARY_OWNED_CROSS_NATIVE_MARKET_SL"
    )
    assert _safe_observer_detail(x)==x.status


@pytest.mark.asyncio
@pytest.mark.parametrize("n",[2,4])
async def test_one_native_stop_failure_latches_entire_portfolio(monkeypatch,n):
    x=await observe(monkeypatch,n,drop_stop=True)
    assert x.status.endswith("_HALT")
    assert x.incident_alert
    assert x.owned_cross_positions_count==0
    assert not x.all_owned_cross_native_market_sl_verified
    assert not x.live_authorization
    assert _status_for_outbox(x)==(
        "OBSERVER_V40_CANARY_SIGNED_GET_OR_ACCOUNT_INVALID"
    )


@pytest.mark.asyncio
async def test_foreign_position_cannot_be_adopted_to_fill_four_slots(monkeypatch):
    x=await observe(monkeypatch,4,foreign_position=True)
    assert x.status.endswith("_HALT")
    assert x.incident_alert
    assert not x.live_authorization


def test_unbacked_2_to_4_claim_never_confers_healthy_guardian_status():
    for fault in (
        dict(owned_cross_positions_count=0),
        dict(owned_cross_positions_count=5),
        dict(all_owned_cross_native_market_sl_verified=False),
        dict(ledger_provenance_verified=False),
        dict(inventory_two_signed_get_bookends=False),
        dict(hedge_mode_verified=False),
        dict(incident_alert=True),
        dict(single_owned_cross_native_sl_verified=True),
        dict(mexc_posts=1),
    ):
        data=dict(
            status="V40_GUARDIAN_TWO_TO_FOUR_OWNED_CROSS_NATIVE_SL_OBSERVED_NONAUTHORIZING",
            owned_cross_positions_count=3,
            all_owned_cross_native_market_sl_verified=True,
            ledger_provenance_verified=True,
            inventory_two_signed_get_bookends=True,
            hedge_mode_verified=True,
            incident_alert=False,
        )
        data.update(fault)
        x=V40CanaryGuardianHeartbeat(**data)
        assert _status_for_outbox(x)==(
            "OBSERVER_V40_CANARY_SIGNED_GET_OR_ACCOUNT_INVALID"
        )


@pytest.mark.asyncio
async def test_flat_unchanged_and_no_hidden_multi_adoption(monkeypatch):
    x=await observe(monkeypatch,0)
    assert x.status=="V40_GUARDIAN_FLAT_ACCOUNT_DOUBLE_GET_STANDBY_NONAUTHORIZING"
    assert x.owned_cross_positions_count==0
    assert not x.all_owned_cross_native_market_sl_verified
    assert _status_for_outbox(x)=="OBSERVER_V40_CANARY_FLAT_DOUBLE_GET"

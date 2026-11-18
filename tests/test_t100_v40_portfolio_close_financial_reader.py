"""Real typed V40 CLOSE_SHORT accounting with conflicting MEXC TP label preserved.

Entirely offline fake HTTP transport. Upstream verified stop/close and strict
order-deal/paginated-funding builders are exercised; nothing writes SQL.
"""
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.t100_live_fill_ledger import build_verified_order_fill_set
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_native_stop_close_recovery import NativeStopRecoveryReport
from app.t100_v40_unreleased_portfolio_native_close import V40PortfolioCloseWitness
from app import t100_v40_portfolio_close_financial_reader as m
from tests.test_t100_live_checkpoint_journal import cp
from tests.test_t100_live_funding_ledger import pages,row as funding_row
from tests.test_t100_live_fill_ledger import order as order_row,fill as deal_row


def order_and_deals(id,side,price,ts):
    order=order_row(
        orderId=id,positionId="10",symbol="SIREN_USDT",
        side=side,dealVol=2,dealAvgPrice=price,
    )
    # MEXC deal IDs are globally unique across entry and close orders,
    # not merely unique within each order's detail endpoint.
    deal=deal_row(
        1 if side==3 else 2,2,price,
        orderId=id,side=side,timestamp=ts,fee=0.1,
    )
    return order,[deal]


def environment(monkeypatch,*,conflict=False):
    previous=replace(
        cp(),contracts=D("2"),
        exchange_order_id="100",stop_order_id="99",
    )
    entry,entry_deals=order_and_deals("100",3,100,2000)
    close,close_deals=order_and_deals("200",2,90,3000)
    close_fills=build_verified_order_fill_set(
        order_id="200",order=close,fills=close_deals,
    )
    term=NativeStopRecoveryReport(
        "EXCHANGE_NATIVE_TERMINAL_POSITIVE",
        native_stop_terminal_verified=True,
        linked_venue_close_filled=True,
        owned_position_closed_verified=True,
        venue_trigger_side_conflict=conflict,
        stop_loss_trigger_side_proven=False,
    )
    witness=V40PortfolioCloseWitness(
        ("V40_PORTFOLIO_CLOSE_EXCHANGE_TP_LABEL_CONFLICT_UNRELEASED"
         if conflict else
         "V40_PORTFOLIO_CLOSE_EXCHANGE_SL_LABEL_OBSERVED_UNRELEASED"),
        closed_position_id="10",exact_close_order_id="200",
        surviving_protected_slots=0,closing_fills=close_fills,
        native_terminal=term,exact_closed_position_history_verified=True,
        native_trigger_label_conflict=conflict,
        portfolio_unchanged_across_bookends=True,
    )
    probe=object.__new__(ReadOnlyMexcProbe)
    reads=[]
    async def by_id(order_id):
        reads.append(("order",order_id))
        return {"100":entry,"200":close}[order_id]
    async def deal_details(order_id):
        reads.append(("deals",order_id))
        return {"100":entry_deals,"200":close_deals}[order_id]
    probe.raw_order_by_id=by_id
    probe.raw_order_deal_details=deal_details
    class Response:
        def raise_for_status(self):pass
        def json(self):
            return {"success":True,"data":[{
                "symbol":"SIREN_USDT","apiAllowed":True,
                "contractSize":"1",
            }]}
    class Public:
        async def get(self,path,*,params=None):
            reads.append(("public",path,params))
            return Response()
    funding_calls=[]
    async def funding_pages(pr,*,position_id):
        assert pr is probe and position_id=="10"
        funding_calls.append(position_id)
        return pages([funding_row(
            1,settleTime=2500,
            positionValue=100,funding=0.1,rate=0.001,
        )])
    monkeypatch.setattr(m,"collect_verified_position_funding_pages",
                        funding_pages)
    port=m.V40NativeCloseFinancialReadPort(
        probe=probe,public_get=Public(),
    )
    return previous,witness,port,reads,funding_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict",[False,True])
async def test_actual_order_deal_funding_accounting_no_stop_label_fabrication(
    monkeypatch,conflict,
):
    previous,witness,port,reads,funding_calls=environment(
        monkeypatch,conflict=conflict,
    )
    result=await port.observe(previous=previous,witness=witness)
    assert result.status.endswith("CURRENT_MULTIPLIER_UNCERTIFIED")
    assert result.accounting.reconciliation.status=="CLOSED_RECONCILED"
    assert result.accounting.reconciliation.opened_contracts==D("2")
    assert result.accounting.reconciliation.closed_contracts==D("2")
    assert result.accounting.reconciliation.realized_gross_usdt==D("20")
    assert result.entry_fills.side=="OPEN_SHORT"
    assert result.close_fills.side=="CLOSE_SHORT"
    assert result.funding.total_count==1
    assert result.funding.double_collection_finality_verified is True
    assert len(funding_calls)==2
    assert reads[:4]==[
        ("order","100"),("deals","100"),("order","200"),("deals","200"),
    ]
    assert result.close_provenance.trigger_side==(
        "VENUE_LABEL_CONFLICT" if conflict else "STOP_LOSS"
    )
    assert result.native_trigger_label_conflict is conflict
    assert result.native_stop_trigger_label_verified is not conflict
    assert not result.historical_contract_multiplier_certified
    assert not result.at_entry_contract_size_independently_verified
    assert not result.live_authorization
    assert result.exchange_posts==result.database_writes==0


@pytest.mark.asyncio
async def test_any_missing_proven_native_close_halts_before_any_private_get(monkeypatch):
    previous,witness,port,reads,_=environment(monkeypatch)
    bad=replace(witness,exact_closed_position_history_verified=False)
    with pytest.raises(
        m.V40CloseFinancialReadHalted,match="POSITIVE_NATIVE_CLOSE_NOT_VERIFIED",
    ):
        await port.observe(previous=previous,witness=bad)
    assert reads==[]


@pytest.mark.asyncio
async def test_conflict_cannot_be_relabelled_as_verified_sl(monkeypatch):
    previous,witness,port,reads,_=environment(monkeypatch,conflict=True)
    invalid=replace(witness,native_trigger_label_conflict=False)
    with pytest.raises(
        m.V40CloseFinancialReadHalted,match="POSITIVE_NATIVE_CLOSE_NOT_VERIFIED",
    ):
        await port.observe(previous=previous,witness=invalid)
    assert not reads


@pytest.mark.asyncio
async def test_signed_close_order_drift_halts_before_funding(monkeypatch):
    previous,witness,port,reads,funding_calls=environment(monkeypatch)
    original=port.probe.raw_order_deal_details
    async def altered(order_id):
        items=await original(order_id)
        if order_id=="200":
            # Keep the signed close order's weighted fill price and volume
            # valid; a changed fee must instead fail the independent bookend.
            return [{**items[0],"fee":0.2}]
        return items
    port.probe.raw_order_deal_details=altered
    with pytest.raises(
        m.V40CloseFinancialReadHalted,match="CLOSE_FILL_BOOKENDS_CHANGED",
    ):
        await port.observe(previous=previous,witness=witness)
    assert funding_calls==[]


@pytest.mark.asyncio
async def test_late_outside_position_funding_event_stops_release(monkeypatch):
    previous,witness,port,reads,_=environment(monkeypatch)
    async def late(pr,*,position_id):
        return pages([funding_row(
            1,settleTime=9999,positionValue=100,
            funding=0.1,rate=0.001,
        )])
    monkeypatch.setattr(m,"collect_verified_position_funding_pages",late)
    with pytest.raises(
        m.V40CloseFinancialReadHalted,match="FUNDING_OUTSIDE_EXACT_FILL_WINDOW",
    ):
        await port.observe(previous=previous,witness=witness)


@pytest.mark.asyncio
async def test_invalid_or_ambiguous_multiplier_never_certifies_exit_accounting(monkeypatch):
    previous,witness,port,reads,_=environment(monkeypatch)
    class BrokenPublic:
        async def get(self,path,*,params=None):
            return SimpleNamespace(
                raise_for_status=lambda:None,
                json=lambda:{"success":True,"data":[
                    {"symbol":"SIREN_USDT","apiAllowed":True,"contractSize":1},
                    {"symbol":"SIREN_USDT","apiAllowed":True,"contractSize":2},
                ]},
            )
    port.public_get=BrokenPublic()
    with pytest.raises(
        m.V40CloseFinancialReadHalted,match="CONTRACT_IDENTITY_UNVERIFIED",
    ):
        await port.observe(previous=previous,witness=witness)



def test_fixture_strict_order_fill_and_funding_are_independently_valid():
    from app.t100_live_funding_ledger import (
        build_verified_closed_position_funding_set,
    )
    from app.t100_live_position_accounting import (
        reconcile_verified_closed_position,
    )
    a,da=order_and_deals("100",3,100,2000)
    b,db=order_and_deals("200",2,90,3000)
    entry=build_verified_order_fill_set(order_id="100",order=a,fills=da)
    close=build_verified_order_fill_set(order_id="200",order=b,fills=db)
    assert entry.side=="OPEN_SHORT" and close.side=="CLOSE_SHORT"
    funds=build_verified_closed_position_funding_set(
        position_id="10",
        first=pages([funding_row(
            1,settleTime=2500,
            positionValue=100,funding=0.1,rate=0.001,
        )]),
        second=pages([funding_row(
            1,settleTime=2500,
            positionValue=100,funding=0.1,rate=0.001,
        )]),
    )
    assert funds.economics_verified
    provenance=m.VerifiedStopCloseProvenance(
        stop_order_id="99",position_id="10",symbol="SIREN_USDT",
        close_order_id="200",contracts=D("2"),
        trigger_side="VENUE_LABEL_CONFLICT",
    )
    accounting=reconcile_verified_closed_position(
        contract_size=D("1"),entry_fills=entry,
        close_provenance=provenance,close_fills=close,funding=funds,
    )
    assert accounting.reconciliation.status=="CLOSED_RECONCILED"

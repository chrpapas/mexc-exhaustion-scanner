"""Final MEXC V40 short-entry POST is a one-shot, frozen SL75 boundary.

Offline only. No real exchange calls or private credentials are used.
"""
import asyncio
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

import app.t100_live_mexc_adapter as m
from app.t100_live_authorization import V40_CANARY_SCOPE
from app.t100_live_execution import EntryIntent


class FakeClient:
    def __init__(self,reply=None):
        self.posts=[]
        self.reply=reply if reply is not None else {"orderId":"123456789"}
    async def _private_post(self,path,body):
        self.posts.append((path,body))
        return self.reply


def intent():
    return EntryIntent.build(
        signal_id=496,symbol="SOL_USDT",reference_price="100",
        notional_usdt="10",contract_size="0.001",
        volume_step="1",min_volume="1",max_volume="1000",
        price_tick="0.01",
    )


def permit():
    return SimpleNamespace(
        scope=V40_CANARY_SCOPE,signal_id=496,symbol="SOL_USDT",
        max_position_notional_usdt=D("12.50"),leverage=1,
    )


def call(i,p):
    client=FakeClient()
    adapter=m.LiveMexcAdapter(client)
    async def send():
        return await adapter.submit_short_with_stop(i,permit=p)
    return client,adapter,send


def test_canary_market_short_cross_one_x_attaches_exact_frozen_stop(monkeypatch):
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda x:x)
    client,_,send=call(intent(),permit())
    ack=asyncio.run(send())
    assert ack=="123456789"
    assert len(client.posts)==1
    path,body=client.posts[0]
    assert path=="/api/v1/private/order/create"
    assert set(body)=={
        "symbol","price","vol","leverage","side","type","openType",
        "positionMode","externalOid","stopLossPrice","lossTrend",
    }
    assert body["symbol"]=="SOL_USDT"
    assert body["side"]==3
    assert body["type"]==5
    assert body["openType"]==2
    assert body["positionMode"]==1
    assert body["leverage"]==1
    assert body["lossTrend"]==1
    assert body["externalOid"]==intent().external_oid
    assert body["stopLossPrice"]=="175.00"


@pytest.mark.parametrize("override",[
    {"external_oid":"wrong-origin"},
    {"stop_price":D("174.99")},
    {"notional_usdt":D("12.51")},
    {"volume_contracts":D("NaN")},
    {"reference_price":D("-100")},
    {"symbol":"BTC_USDT"},
    {"symbol":"sol_USDT"},
    {"signal_id":0},
])
def test_bad_frozen_entry_never_posts(monkeypatch,override):
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda x:x)
    client,_,send=call(replace(intent(),**override),permit())
    with pytest.raises(RuntimeError,match="NO_POST"):
        asyncio.run(send())
    assert client.posts==[]


@pytest.mark.parametrize("change",[
    {"signal_id":1234},
    {"symbol":"ETH_USDT"},
    {"leverage":2},
    {"max_position_notional_usdt":D("4")},
])
def test_bad_permit_never_posts(monkeypatch,change):
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda x:x)
    p=permit()
    for k,v in change.items():setattr(p,k,v)
    client,_,send=call(intent(),p)
    with pytest.raises(RuntimeError,match="NO_POST"):
        asyncio.run(send())
    assert client.posts==[]


def test_uncertified_adapter_never_posts_even_with_valid_intent():
    # The shipped production policy is deliberately NOT_IMPLEMENTED.
    client,_,send=call(intent(),permit())
    with pytest.raises(Exception):
        asyncio.run(send())
    assert client.posts==[]


@pytest.mark.parametrize("reply",[
    {},{"orderId":0},{"orderId":True},{"orderId":"0"},
    {"orderId":"-1"},{"orderId":"abc"},
])
def test_post_ack_invalid_caller_must_reconcile_never_auto_retry(
    monkeypatch,reply,
):
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda x:x)
    client=FakeClient(reply=reply)
    adapter=m.LiveMexcAdapter(client)
    with pytest.raises(RuntimeError,match="orderId"):
        asyncio.run(adapter.submit_short_with_stop(intent(),permit=permit()))
    # One and only one POST attempt, even after an invalid ACK.
    assert len(client.posts)==1

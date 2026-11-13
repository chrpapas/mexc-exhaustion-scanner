"""Emergency close is complete only with exact filled order and flat bookends."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.t100_v40_emergency_postflight import assess_emergency_close_postflight
from tests.test_t100_v40_emergency_close import frame,target


def flat(**changes):
    x=frame(
        positions=[],symbol_positions=[],
        stops=[],symbol_stops=[],
    )
    return replace(x,**changes)


def order(**changes):
    t=target()
    raw=dict(
        orderId="794000000000000001",
        externalOid=t.close_external_oid,
        positionId=t.position_id,symbol=t.symbol,
        side=2,orderType=5,state=3,openType=2,
        dealVol="1",dealAvgPrice="101",feeCurrency="USDT",
        errorCode=0,
    )
    raw.update(changes)
    return raw


def fills(**changes):
    t=target()
    raw=dict(
        id="1234",orderId="794000000000000001",symbol=t.symbol,
        side=2,vol="1",price="101",fee="0.001",
        feeCurrency="USDT",timestamp=1790000000000,category=1,
    )
    raw.update(changes)
    return [raw]


def run(*,first=None,second=None,order_row=None,deal_rows=None):
    return assess_emergency_close_postflight(
        target=target(),
        close_order=order() if order_row is None else order_row,
        deals=fills() if deal_rows is None else deal_rows,
        flat_first=flat() if first is None else first,
        flat_second=flat() if second is None else second,
    )


def test_exact_market_close_full_fill_and_two_complete_flat_gets_verified():
    r=run()
    assert r.status=="V40_EMERGENCY_CLOSE_ORDER_AND_FLAT_ACCOUNT_DOUBLE_GET_VERIFIED"
    assert r.exact_close_order_id=="794000000000000001"
    assert r.exact_close_fill_set.side=="CLOSE_SHORT"
    assert r.exact_close_fill_set.deal_volume==D("1")
    assert r.exact_close_fills_verified
    assert r.full_account_flat_double_get_verified
    assert r.journal_close_persisted is False
    assert r.entry_allowed is False and r.live_authorization is False
    assert r.any_further_close_post_allowed is False
    assert r.mexc_posts==0 and r.database_writes==0


@pytest.mark.parametrize("changed,code",[
    ({"side":3},"V40_EMERGENCY_POSTFLIGHT_CLOSE_IDENTITY_OR_FILL_DRIFT"),
    ({"positionId":"999"},"V40_EMERGENCY_POSTFLIGHT_CLOSE_IDENTITY_OR_FILL_DRIFT"),
    ({"externalOid":"WRONG"},"V40_EMERGENCY_POSTFLIGHT_CLOSE_IDENTITY_OR_FILL_DRIFT"),
    ({"state":2},"V40_EMERGENCY_POSTFLIGHT_CLOSE_IDENTITY_OR_FILL_DRIFT"),
    ({"dealVol":"0.5"},"V40_EMERGENCY_POSTFLIGHT_CLOSE_IDENTITY_OR_FILL_DRIFT"),
    ({"orderType":1},"V40_EMERGENCY_POSTFLIGHT_CLOSE_IDENTITY_OR_FILL_DRIFT"),
    ({"openType":1},"V40_EMERGENCY_POSTFLIGHT_CLOSE_IDENTITY_OR_FILL_DRIFT"),
])
def test_no_partial_close_or_wrong_side_is_ever_certified(changed,code):
    r=run(order_row=order(**changed))
    assert r.status==code
    assert not r.exact_position_exit_verified
    assert not r.journal_close_persisted


@pytest.mark.parametrize("bad",[
    {"side":3},{"orderId":"888"},{"vol":"0.5"},
    {"symbol":"ETH_USDT"},{"feeCurrency":"EUR"},
    {"price":"99"},{"category":2},
])
def test_wrong_deal_lineage_price_or_quantity_fails_safe(bad):
    r=run(deal_rows=fills(**bad))
    assert r.status=="V40_EMERGENCY_POSTFLIGHT_EXACT_CLOSE_GET_UNVERIFIED"
    assert not r.exact_close_fills_verified


@pytest.mark.parametrize("changed",[
    {"positions":[{"positionId":"9801"}]},
    {"symbol_positions":[{"positionId":"9801"}]},
    {"stops":[{"id":"123"}]},
    {"hedge_mode":2},
    {"counts":{**flat().counts,"sumCount":1,"limitOrderCount":1}},
])
def test_any_nonflat_or_outstanding_order_blocks_verified_exit(changed):
    r=run(second=flat(**changed))
    assert r.status=="V40_EMERGENCY_POSTFLIGHT_ACCOUNT_NOT_FLAT"
    assert r.journal_close_persisted is False

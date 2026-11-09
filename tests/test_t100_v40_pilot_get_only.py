"""No-network tests of explicit-account MEXC GET-only pilot preflight."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from scripts.t100_v40_pilot_get_only import (
    PilotInventory,PilotPreflightHalted,
    evaluate_pilot_flat_bookends,
    observe_live_pilot,
)


def sample():
    return PilotInventory(
        positions=[],stops=[],
        counts={
            "sumCount":0,"stopOrderCount":0,
            "limitOrderCount":0,"planOrderCount":0,
            "trackOrderCount":0,
        },
        assets=[{
            "currency":"USDT","equity":"49.9",
            "availableBalance":"49.9",
            "cashBalance":"49.9",
            "positionMargin":"0","unrealized":"0",
            "frozenBalance":"0","bonus":"0",
            "debtAmount":"0","contributeMarginAmount":"0",
        }],
        position_mode=1,
    )


def test_two_flat_hedged_signed_get_bookends_not_live_authorization():
    got=evaluate_pilot_flat_bookends(sample(),sample())
    assert got.status=="PILOT_FLAT_HEDGE_SIGNED_GET_BOOKENDS_NONAUTHORIZING"
    assert got.account_equity_usdt=="49.9"
    assert got.available_balance_usdt=="49.9"
    assert got.hedge_mode_observed
    assert got.inventory_bookends_equal
    assert not got.account_scope_independently_verified
    assert not got.cross_margin_collateral_verified
    assert not got.private_post_permission_verified
    assert not got.exact_native_stop_lifecycle_verified
    assert not got.live_authorization
    assert got.mexc_posts==0
    assert got.db_mutations==0


@pytest.mark.parametrize("fault",[
    "open_position","open_stop","pending_order","one_way_mode",
    "negative_equity","missing_usdt","duplicate_usdt",
    "missing_collateral_component","available_above_equity",
    "nonzero_position_margin","nonzero_unrealized","nonzero_debt",
    "nonfinite_equity","boolean_account_value",
])
def test_nonflat_or_ambiguous_pilot_halts(fault):
    a=sample()
    b=deepcopy(a)
    if fault=="open_position":b.positions=[{"positionId":"10"}]
    if fault=="open_stop":b.stops=[{"id":"20"}]
    if fault=="pending_order":
        b.counts["sumCount"]=b.counts["stopOrderCount"]=1
    if fault=="one_way_mode":b.position_mode=2
    if fault=="negative_equity":b.assets[0]["equity"]="-1"
    if fault=="missing_usdt":b.assets=[]
    if fault=="duplicate_usdt":b.assets.append(deepcopy(b.assets[0]))
    if fault=="missing_collateral_component":
        del b.assets[0]["frozenBalance"]
    if fault=="available_above_equity":
        b.assets[0]["availableBalance"]="100"
    if fault=="nonzero_position_margin":
        b.assets[0]["positionMargin"]="1"
    if fault=="nonzero_unrealized":
        b.assets[0]["unrealized"]="2"
    if fault=="nonzero_debt":
        b.assets[0]["debtAmount"]="0.1"
    if fault=="nonfinite_equity":b.assets[0]["equity"]="NaN"
    if fault=="boolean_account_value":b.assets[0]["equity"]=True
    with pytest.raises(PilotPreflightHalted):
        evaluate_pilot_flat_bookends(a,b)


def test_same_flat_count_but_changed_collateral_between_books_halts():
    a=sample()
    b=deepcopy(a)
    b.assets[0]["frozenBalance"]="0.1"
    with pytest.raises(PilotPreflightHalted,match="BOOKEND_CHANGED"):
        evaluate_pilot_flat_bookends(a,b)


class FakePrivateGet:
    def __init__(self):
        self.calls=[]
        self.closed=False
        self.mode={"positionMode":1}
    async def _private_get(self,path,params=None):
        self.calls.append((path,params))
        raw=sample()
        answers={
            "/api/v1/private/position/open_positions":raw.positions,
            "/api/v1/private/stoporder/open_orders":raw.stops,
            "/api/v1/private/order/open_order_total_count":raw.counts,
            "/api/v1/private/account/assets":raw.assets,
            "/api/v1/private/position/position_mode":self.mode,
        }
        return deepcopy(answers[path])
    async def close(self):
        self.closed=True


def test_two_cycles_use_exactly_ten_signed_gets_and_no_post(monkeypatch):
    import scripts.t100_v40_pilot_get_only as pilot
    transport=FakePrivateGet()
    monkeypatch.setattr(
        pilot,"KeychainSignedGetOnly",
        lambda **kw:transport,
    )
    result=asyncio.run(observe_live_pilot(key="test",secret="test"))
    assert result.samples==2
    assert len(transport.calls)==10
    assert all(params is None for _,params in transport.calls)
    assert transport.closed
    assert result.mexc_posts==0


def test_probe_invalid_position_mode_halts_and_closes_transport(monkeypatch):
    import scripts.t100_v40_pilot_get_only as pilot
    transport=FakePrivateGet()
    transport.mode={"mode":1}
    monkeypatch.setattr(
        pilot,"KeychainSignedGetOnly",
        lambda **kw:transport,
    )
    with pytest.raises(PilotPreflightHalted,match="SIGNED_GET_UNVERIFIED"):
        asyncio.run(observe_live_pilot(key="test",secret="test"))
    assert transport.closed


def test_non_allowlisted_signed_endpoint_rejected_before_http():
    from scripts.t100_v40_pilot_get_only import KeychainSignedGetOnly
    client=KeychainSignedGetOnly(key="fake",secret="fake")
    async def test():
        try:
            for path in (
                "/api/v1/private/order/submit",
                "/api/v1/private/stoporder/change_plan_price",
                "/api/v1/private/position/change_leverage",
            ):
                with pytest.raises(PilotPreflightHalted,match="NOT_ALLOWLISTED"):
                    await client._private_get(path)
        finally:
            await client.close()
    asyncio.run(test())

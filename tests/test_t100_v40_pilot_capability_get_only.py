"""Offline, no-network regression for V40 pilot public/private GET capability.

A valid public contract and private current short-tier GET NEVER prove
Futures order-write permission, exchange stop amendment or exit execution.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from scripts.t100_v40_pilot_capability_get_only import (
    PilotCapabilityHalted,assess_pilot_capability,
    observe_pilot_capability,
)


SYMBOL="BTC_USDT"


def contract():
    return {
        "symbol":SYMBOL,"apiAllowed":True,"state":0,
        "futureType":1,"positionOpenType":3,
        "quoteCoin":"USDT","settleCoin":"USDT",
        "minVol":"1","maxVol":"10000","volUnit":"1",
        "priceUnit":"0.1","contractSize":"0.0001",
    }


def tiers():
    return {SYMBOL:[
        {"symbol":SYMBOL,"positionType":1,"level":1,
         "openType":2,"leverage":1,"maxVol":"10000"},
        {"symbol":SYMBOL,"positionType":2,"level":1,
         "openType":2,"leverage":1,"maxVol":"10000"},
    ]}


def leverages():
    # MEXC official position/leverage sample omits symbol in rows.
    return [
        {"positionType":1,"openType":1,"leverage":2},
        {"positionType":2,"openType":2,"leverage":1},
    ]


def assess(*,c=None,risk=None,lev=None,flat=True):
    return assess_pilot_capability(
        symbol=SYMBOL,contract=contract() if c is None else c,
        private_risk=tiers() if risk is None else risk,
        private_leverage=leverages() if lev is None else lev,
        flat_preflight_ok=flat,
    )


def test_valid_public_contract_private_tier_is_never_trade_permission():
    r=assess()
    assert r.status==(
        "PILOT_PUBLIC_CONTRACT_AND_PRIVATE_SHORT_CROSS_TIER_OBSERVED_NONAUTHORIZING"
    )
    assert r.flat_hedge_account_bookended
    assert r.public_futures_contract_eligible_observed
    assert r.current_private_short_cross_1x_tier_observed
    assert r.current_private_short_risk_limit_observed
    assert r.private_short_position_leverage_observed
    assert r.short_margin_mode_observed=="CROSS"
    assert r.short_leverage_observed==1
    assert r.one_contract_on_public_lot_grid
    assert r.current_short_tier_supports_one_contract
    assert r.current_contract_tick_observed
    assert not r.api_key_futures_write_permission_verified
    assert not r.api_key_subaccount_identity_independently_verified
    assert not r.native_market_stop_amendment_verified
    assert not r.native_stop_terminal_execution_verified
    assert not r.future_stressed_risk_tiers_verified
    assert not r.cross_collateral_verified
    assert not r.account_snapshot_atomicity_verified
    assert not r.live_authorization
    assert not r.entry_allowed
    assert r.mexc_posts==0
    assert r.db_mutations==0


def test_if_flat_evidence_missing_do_not_evaluate_contract_or_private_risk():
    r=assess(c={},risk={},flat=False)
    assert r.status=="PILOT_FLAT_GET_BOOKENDS_REQUIRED_HALT"
    assert not r.flat_hedge_account_bookended
    assert not r.current_private_short_cross_1x_tier_observed


@pytest.mark.parametrize("field,value",[
    ("symbol","ETH_USDT"),
    ("apiAllowed",False),
    ("state",3),
    ("futureType",2),
    ("positionOpenType",1),
    ("quoteCoin","BTC"),
    ("settleCoin","BTC"),
])
def test_ineligible_public_contract_blocks_but_never_infers_key_permission(field,value):
    c=contract()
    c[field]=value
    r=assess(c=c)
    assert r.status=="PILOT_PUBLIC_CONTRACT_NOT_ELIGIBLE_HALT"
    assert not r.api_key_futures_write_permission_verified
    assert not r.live_authorization


@pytest.mark.parametrize("field,value",[
    ("volUnit","0"),("maxVol","0"),
    ("priceUnit","NaN"),("contractSize",None),
])
def test_contract_size_and_tick_schema_fail_closed(field,value):
    c=contract();c[field]=value
    r=assess(c=c)
    assert r.status=="PILOT_PUBLIC_CONTRACT_GRID_INVALID_HALT"


@pytest.mark.parametrize("field,value",[
    ("minVol","2"),("volUnit","2"),("maxVol","0.5"),
])
def test_actual_one_contract_min_lot_requirement(field,value):
    c=contract();c[field]=value
    r=assess(c=c)
    assert r.status in (
        "PILOT_ONE_CONTRACT_NOT_ON_LOT_GRID_HALT",
        "PILOT_PUBLIC_CONTRACT_GRID_INVALID_HALT",
    )
    assert not r.current_short_tier_supports_one_contract


@pytest.mark.parametrize("field,value,status",[
    ("openType",1,"PILOT_PRIVATE_RISK_VS_LEVERAGE_CONFLICT_HALT"),
    ("leverage",2,"PILOT_PRIVATE_RISK_VS_LEVERAGE_CONFLICT_HALT"),
    ("level",0,"PILOT_PRIVATE_SHORT_RISK_TIER_INVALID_HALT"),
])
def test_risk_tier_mismatch_never_overrides_dedicated_leverage_get(field,value,status):
    t=tiers();t[SYMBOL][1][field]=value
    r=assess(risk=t)
    assert r.status==status
    assert not r.current_private_short_cross_1x_tier_observed
    assert r.entry_allowed is False


@pytest.mark.parametrize("field,value,mode,leverage",[
    ("openType",1,"ISOLATED",1),
    ("leverage",2,"CROSS",2),
])
def test_dedicated_position_leverage_get_reports_actual_wrong_short_setting(
    field,value,mode,leverage,
):
    lev=leverages();lev[1][field]=value
    r=assess(lev=lev)
    assert r.status=="PILOT_PRIVATE_SHORT_CONFIG_NOT_CROSS_ONE_X_HALT"
    assert r.private_short_position_leverage_observed
    assert r.short_margin_mode_observed==mode
    assert r.short_leverage_observed==leverage
    assert not r.current_private_short_cross_1x_tier_observed
    assert not r.live_authorization


def test_legacy_risk_limit_without_open_type_or_leverage_uses_correct_endpoint():
    t=tiers()
    for row in t[SYMBOL]:
        row.pop("openType")
        row.pop("leverage")
    r=assess(risk=t)
    assert r.status.endswith("_OBSERVED_NONAUTHORIZING")
    assert r.private_risk_limit_mode_fields_missing is True
    assert r.private_short_position_leverage_observed
    assert r.current_private_short_cross_1x_tier_observed
    assert not r.entry_allowed


@pytest.mark.parametrize("bad",[
    [],
    [{"positionType":1,"openType":2,"leverage":1}],
    [{"positionType":2,"openType":1,"leverage":0}],
    [{"positionType":2,"openType":2,"leverage":True}],
    [{"positionType":2,"openType":2,"leverage":1,"symbol":"ETH_USDT"}],
    [{"positionType":2,"openType":2,"leverage":1}]*2,
])
def test_leverage_missing_duplicate_or_malformed_never_authorizes(bad):
    r=assess(lev=bad)
    assert r.status in (
        "PILOT_PRIVATE_LEVERAGE_ROWS_UNVERIFIED_HALT",
        "PILOT_PRIVATE_SHORT_LEVERAGE_MISSING_HALT",
    )
    assert not r.current_private_short_cross_1x_tier_observed
    assert not r.live_authorization


@pytest.mark.parametrize("risk",[
    {},{"ETH_USDT":[{"symbol":"ETH_USDT","positionType":2}]},
    {SYMBOL:[]},{SYMBOL:[{"symbol":"BAD_USDT","positionType":2}]},
])
def test_private_risk_scope_is_exact_or_fails_closed(risk):
    r=assess(risk=risk)
    assert r.status!=(
        "PILOT_PUBLIC_CONTRACT_AND_PRIVATE_SHORT_CROSS_TIER_OBSERVED_NONAUTHORIZING"
    )
    assert not r.live_authorization


def test_duplicate_short_risk_side_is_rejected():
    t=tiers()
    t[SYMBOL].append(dict(t[SYMBOL][1]))
    assert assess(risk=t).status=="PILOT_PRIVATE_SHORT_TIER_NOT_UNIQUE_HALT"


def test_private_short_max_vol_below_one_is_not_whitelisted():
    t=tiers()
    t[SYMBOL][1]["maxVol"]="0.5"
    r=assess(risk=t)
    assert r.status=="PILOT_PRIVATE_SHORT_TIER_BELOW_ONE_CONTRACT_HALT"
    assert r.current_private_short_cross_1x_tier_observed
    assert not r.current_short_tier_supports_one_contract


def test_bad_symbol_never_opened_as_arbitrary_url():
    with pytest.raises(PilotCapabilityHalted,match="IDENTITY_INVALID"):
        assess_pilot_capability(
            symbol="../../private/order/submit",
            contract=contract(),private_risk=tiers(),
            private_leverage=leverages(),flat_preflight_ok=True,
        )


def test_live_observation_aborts_if_account_preflight_not_flat(monkeypatch):
    import scripts.t100_v40_pilot_capability_get_only as m
    from scripts.t100_v40_pilot_get_only import PilotGetOnlyResult

    async def flat(**kw):
        return PilotGetOnlyResult(status="PILOT_PENDING_ORDERS_PRESENT_HALT")
    monkeypatch.setattr(m,"observe_live_pilot",flat)
    class NoSigned:
        def __init__(self,**kw):raise AssertionError("risk GET never allowed")
    monkeypatch.setattr(m,"KeychainSignedGetOnly",NoSigned)
    r=asyncio.run(observe_pilot_capability(
        key="dummy",secret="dummy",symbol=SYMBOL,
    ))
    assert r.status=="PILOT_FLAT_GET_BOOKENDS_REQUIRED_HALT"
    assert not r.entry_allowed


def test_tight_signed_risk_get_and_public_country_only(monkeypatch):
    import scripts.t100_v40_pilot_capability_get_only as m
    from scripts.t100_v40_pilot_get_only import PilotGetOnlyResult

    calls=[]
    async def flat(**kw):
        calls.append("FLAT")
        return PilotGetOnlyResult(
            status="PILOT_FLAT_HEDGE_SIGNED_GET_BOOKENDS_NONAUTHORIZING",
            samples=2,position_count=0,native_stop_count=0,
            hedge_mode_observed=True,inventory_bookends_equal=True,
        )
    monkeypatch.setattr(m,"observe_live_pilot",flat)

    class Signed:
        async def _private_get(self,path,params=None):
            calls.append(("PRIVATE_GET",path,params))
            assert path in (
                "/api/v1/private/account/risk_limit",
                "/api/v1/private/position/leverage",
            )
            assert params=={"symbol":SYMBOL}
            return (
                tiers() if path.endswith("/risk_limit") else leverages()
            )
        async def close(self): calls.append("CLOSE")

    monkeypatch.setattr(m,"KeychainSignedGetOnly",lambda **kw:Signed())

    class Response:
        def raise_for_status(self):pass
        def json(self):
            return {"success":True,"code":0,"data":contract()}
    class Client:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,path,params):
            calls.append(("PUBLIC_GET",path,params))
            assert path=="/api/v1/contract/detail/country"
            assert params=={"symbol":SYMBOL}
            return Response()
    monkeypatch.setattr(m.httpx,"AsyncClient",lambda **kw:Client())
    result=asyncio.run(observe_pilot_capability(
        key="dummy",secret="dummy",symbol=SYMBOL,
    ))
    assert result.status.endswith("_OBSERVED_NONAUTHORIZING")
    assert calls==[
        "FLAT",
        ("PRIVATE_GET","/api/v1/private/account/risk_limit",{"symbol":SYMBOL}),
        ("PRIVATE_GET","/api/v1/private/position/leverage",{"symbol":SYMBOL}),
        ("PRIVATE_GET","/api/v1/private/account/risk_limit",{"symbol":SYMBOL}),
        ("PRIVATE_GET","/api/v1/private/position/leverage",{"symbol":SYMBOL}),
        "CLOSE",
        ("PUBLIC_GET","/api/v1/contract/detail/country",{"symbol":SYMBOL}),
    ]
    assert not result.live_authorization


def test_private_leverage_bookend_drift_never_infers_cross_1x(monkeypatch):
    import scripts.t100_v40_pilot_capability_get_only as m
    from scripts.t100_v40_pilot_get_only import PilotGetOnlyResult

    async def flat(**kw):
        return PilotGetOnlyResult(
            status="PILOT_FLAT_HEDGE_SIGNED_GET_BOOKENDS_NONAUTHORIZING",
            samples=2,position_count=0,native_stop_count=0,
            hedge_mode_observed=True,inventory_bookends_equal=True,
        )
    monkeypatch.setattr(m,"observe_live_pilot",flat)
    class Signed:
        calls=0
        async def _private_get(self,path,params=None):
            assert params=={"symbol":SYMBOL}
            if path.endswith("/risk_limit"):
                return tiers()
            self.calls+=1
            rows=leverages()
            if self.calls==2:
                rows[1]["leverage"]=2
            return rows
        async def close(self):pass
    monkeypatch.setattr(m,"KeychainSignedGetOnly",lambda **kw:Signed())
    class PublicForbidden:
        def __init__(self,**kw):
            raise AssertionError("No public GET when private settings drift")
    monkeypatch.setattr(m.httpx,"AsyncClient",PublicForbidden)
    result=asyncio.run(observe_pilot_capability(
        key="fake",secret="fake",symbol=SYMBOL,
    ))
    assert result.status=="PILOT_PRIVATE_CAPABILITY_GET_DRIFT_HALT"
    assert not result.live_authorization
    assert not result.entry_allowed
    assert result.mexc_posts==0


def test_signed_leverage_get_failure_has_specific_redacted_status(monkeypatch):
    import scripts.t100_v40_pilot_capability_get_only as m
    from scripts.t100_v40_pilot_get_only import PilotGetOnlyResult

    async def flat(**kw):
        return PilotGetOnlyResult(
            status="PILOT_FLAT_HEDGE_SIGNED_GET_BOOKENDS_NONAUTHORIZING",
            samples=2,position_count=0,native_stop_count=0,
            hedge_mode_observed=True,inventory_bookends_equal=True,
        )
    monkeypatch.setattr(m,"observe_live_pilot",flat)
    class Signed:
        async def _private_get(self,path,params=None):
            if path.endswith("/leverage"):
                raise RuntimeError("PRIVATE_SECRET")
            return tiers()
        async def close(self):pass
    monkeypatch.setattr(m,"KeychainSignedGetOnly",lambda **kw:Signed())
    result=asyncio.run(observe_pilot_capability(
        key="fake",secret="fake",symbol=SYMBOL,
    ))
    assert result.status=="PILOT_PRIVATE_LEVERAGE_GET_UNVERIFIED_HALT"
    assert "PRIVATE_SECRET" not in result.status
    assert not result.live_authorization

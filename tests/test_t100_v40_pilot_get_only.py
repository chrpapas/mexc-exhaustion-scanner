"""No-network tests of explicit-account MEXC GET-only pilot preflight."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace

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
    if fault=="open_position":b=replace(b,positions=[{"positionId":"10"}])
    if fault=="open_stop":b=replace(b,stops=[{"id":"20"}])
    if fault=="pending_order":
        b.counts["sumCount"]=b.counts["stopOrderCount"]=1
    if fault=="one_way_mode":b=replace(b,position_mode=2)
    if fault=="negative_equity":b.assets[0]["equity"]="-1"
    if fault=="missing_usdt":b=replace(b,assets=[])
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
    with pytest.raises(PilotPreflightHalted) as error:
        evaluate_pilot_flat_bookends(a,b)
    expected={
        "open_position":"PILOT_OPEN_POSITIONS_PRESENT_HALT",
        "open_stop":"PILOT_ACTIVE_STOP_ORDERS_PRESENT_HALT",
        "pending_order":"PILOT_PENDING_ORDERS_PRESENT_HALT",
        "one_way_mode":"PILOT_HEDGE_MODE_NOT_OBSERVED_HALT",
        "negative_equity":"USDT_ASSET_FIELD_INVALID",
        "missing_usdt":"PILOT_USDT_ASSET_MISSING_OR_DUPLICATE",
        "duplicate_usdt":"PILOT_USDT_ASSET_MISSING_OR_DUPLICATE",
        "missing_collateral_component":"PILOT_USDT_ASSET_FIELDS_MISSING",
        "available_above_equity":"PILOT_AVAILABLE_EQUITY_GAP_EXCEEDS_DISPLAY_PRECISION_HALT",
        "nonzero_position_margin":"PILOT_POSITION_MARGIN_NONZERO_HALT",
        "nonzero_unrealized":"PILOT_UNREALIZED_PNL_NONZERO_HALT",
        "nonzero_debt":"PILOT_DEBT_NONZERO_HALT",
        "nonfinite_equity":"USDT_ASSET_FIELD_INVALID",
        "boolean_account_value":"USDT_ASSET_FIELD_INVALID",
    }
    assert str(error.value)==expected[fault]
    assert "SECRET" not in str(error.value)


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


def test_multiple_unsafe_conditions_prioritize_open_position_over_mode_or_orders():
    a=sample()
    b=replace(
        deepcopy(a),
        positions=[{"positionId":"PRIVATE_ID_NOT_FOR_OUTPUT"}],
        stops=[{"id":"PRIVATE_STOP_ID_NOT_FOR_OUTPUT"}],
        position_mode=2,
    )
    with pytest.raises(PilotPreflightHalted) as exc:
        evaluate_pilot_flat_bookends(a,b)
    assert str(exc.value)=="PILOT_OPEN_POSITIONS_PRESENT_HALT"
    assert "PRIVATE_" not in str(exc.value)


def test_active_stop_diagnosed_before_pending_order_counts():
    a=sample()
    b=replace(deepcopy(a),stops=[{"id":"PRIVATE_STOP_ID"}])
    b.counts["stopOrderCount"]=1
    b.counts["sumCount"]=1
    with pytest.raises(PilotPreflightHalted,match="ACTIVE_STOP_ORDERS_PRESENT"):
        evaluate_pilot_flat_bookends(a,b)


def test_malformed_order_count_sum_is_not_interpreted_as_flat():
    a=sample()
    b=deepcopy(a)
    b.counts["sumCount"]=1
    with pytest.raises(PilotPreflightHalted,match="COUNT_SUM_MISMATCH"):
        evaluate_pilot_flat_bookends(a,b)


def test_zero_equity_reports_specific_non_authorizing_reason():
    a=sample()
    b=deepcopy(a)
    b.assets[0]["equity"]="0"
    with pytest.raises(PilotPreflightHalted,match="PILOT_USDT_EQUITY_NONPOSITIVE_HALT"):
        evaluate_pilot_flat_bookends(a,b)


@pytest.mark.parametrize("field,expected",[
    ("positionMargin","PILOT_POSITION_MARGIN_NONZERO_HALT"),
    ("unrealized","PILOT_UNREALIZED_PNL_NONZERO_HALT"),
    ("debtAmount","PILOT_DEBT_NONZERO_HALT"),
])
def test_after_position_close_residual_account_fields_stay_blocking(field,expected):
    a=sample()
    b=deepcopy(a)
    b.assets[0][field]="0.00000001"
    with pytest.raises(PilotPreflightHalted) as exc:
        evaluate_pilot_flat_bookends(a,b)
    assert str(exc.value)==expected
    assert "0.00000001" not in str(exc.value)


def test_negative_unrealized_pnl_is_a_signed_nonzero_blocker_not_bad_schema():
    a=sample()
    b=deepcopy(a)
    b.assets[0]["unrealized"]="-0.00000001"
    with pytest.raises(PilotPreflightHalted) as exc:
        evaluate_pilot_flat_bookends(a,b)
    assert str(exc.value)=="PILOT_UNREALIZED_PNL_NONZERO_HALT"


def test_mexc_official_documented_micro_roundoff_reconciles_without_authorizing():
    # Real official MEXC example of a flat 0-margin USDT wallet:
    # availableBalance 32.80984793658 > equity 32.80984793.
    a=sample()
    b=deepcopy(a)
    for row in (a.assets[0],b.assets[0]):
        row["equity"]="32.80984793"
        row["cashBalance"]="32.80984793"
        row["availableBalance"]="32.80984793658"
    out=evaluate_pilot_flat_bookends(a,b)
    assert out.status=="PILOT_FLAT_HEDGE_SIGNED_GET_BOOKENDS_NONAUTHORIZING"
    assert out.available_equity_roundoff_observed
    assert out.account_equity_usdt=="32.80984793"
    assert out.available_balance_usdt=="32.80984793658"
    assert not out.cross_margin_collateral_verified
    assert not out.live_authorization
    assert out.mexc_posts==0


def test_display_precision_one_quantum_accepted_but_not_more():
    a=sample()
    b=deepcopy(a)
    for row in (a.assets[0],b.assets[0]):
        row["equity"]="50"
        row["cashBalance"]="50"
        row["availableBalance"]="50.00000001"
    assert evaluate_pilot_flat_bookends(
        a,b
    ).available_equity_roundoff_observed
    b.assets[0]["availableBalance"]="50.00000001001"
    with pytest.raises(PilotPreflightHalted,match="EXCEEDS_DISPLAY_PRECISION"):
        evaluate_pilot_flat_bookends(a,b)


def test_small_available_equity_gap_without_cash_equity_parity_still_halts():
    a=sample()
    b=deepcopy(a)
    for row in (a.assets[0],b.assets[0]):
        row["equity"]="50"
        row["availableBalance"]="50.000000005"
        row["cashBalance"]="49.0"
    with pytest.raises(PilotPreflightHalted,match="CASH_EQUITY_PRECISION_CROSSCHECK"):
        evaluate_pilot_flat_bookends(a,b)


def test_actual_material_profit_does_not_create_unbounded_equity_exception():
    a=sample()
    b=deepcopy(a)
    for row in (a.assets[0],b.assets[0]):
        row["equity"]="85"
        row["cashBalance"]="85"
        row["availableBalance"]="85.10"
    with pytest.raises(PilotPreflightHalted,match="EXCEEDS_DISPLAY_PRECISION"):
        evaluate_pilot_flat_bookends(a,b)


def test_symbol_scoped_risk_limit_get_signs_exact_urlencoded_parameters(monkeypatch):
    import hashlib
    import hmac
    from scripts.t100_v40_pilot_get_only import KeychainSignedGetOnly
    import scripts.t100_v40_pilot_get_only as pilot

    class Response:
        def raise_for_status(self):pass
        def json(self):return {"success":True,"code":0,"data":{"BTC_USDT":[]}}

    class Client:
        def __init__(self):self.calls=[]
        async def get(self,path,*,params=None,headers=None):
            self.calls.append((path,params,headers))
            return Response()
        async def aclose(self):pass

    monkeypatch.setattr(pilot.time,"time",lambda:1712345.678)
    client=KeychainSignedGetOnly(key="fakekey",secret="fakesecret")
    mock=Client()
    client._client=mock

    async def invoke():
        try:
            result=await client._private_get(
                "/api/v1/private/account/risk_limit",
                {"symbol":"BTC_USDT"},
            )
            assert result=={"BTC_USDT":[]}
            for path,params in (
                ("/api/v1/private/account/risk_limit",{"symbol":"BTC_USDT","other":"x"}),
                ("/api/v1/private/account/assets",{"symbol":"BTC_USDT"}),
                ("/api/v1/private/order/submit",{"symbol":"BTC_USDT"}),
            ):
                with pytest.raises(PilotPreflightHalted,match="GET_ONLY_PATH_NOT_ALLOWLISTED"):
                    await client._private_get(path,params)
        finally:
            await client.close()
    asyncio.run(invoke())
    assert len(mock.calls)==1
    path,params,headers=mock.calls[0]
    assert path=="/api/v1/private/account/risk_limit"
    assert params=={"symbol":"BTC_USDT"}
    stamp=str(int(1712345.678*1000))
    expected=hmac.new(
        b"fakesecret",f"fakekey{stamp}symbol=BTC_USDT".encode(),hashlib.sha256,
    ).hexdigest()
    assert headers["Signature"]==expected
    assert headers["Request-Time"]==stamp

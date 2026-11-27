"""Strict real-shape GET-only V40 depth/risk witness tests (all mocked)."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal as D

import pytest

from app.t100_live_execution import EntryIntent
from app.t100_live_readonly_probe import ReadOnlyMexcProbe,VenueReadHalted
from app.t100_live_v40_candidate_market_witness import (
    MexcV40CandidateMarketWitness,V40CandidateMarketHalted,
    _DEPTH_PREFIX,_COUNTRY_PATH,
)
from tests.test_t100_live_v40_entry_coordinator import candidate

SYM="EXAMPLE_USDT"
START=1_000_000
END=1_000_500


class Private:
    def __init__(self):
        self.positions=[]
        self.stops=[]
        self.assets=[{
            "currency":"USDT","cashBalance":"5000",
            "equity":"5000","availableBalance":"4900",
            "positionMargin":"0","unrealized":"0",
            "frozenBalance":"0","bonus":"0",
            "debtAmount":"0","contributeMarginAmount":"0",
        }]
        self.counts={
            "sumCount":0,"stopOrderCount":0,"limitOrderCount":0,
            "planOrderCount":0,"trackOrderCount":0,
        }
        self.mode={"positionMode":1}
        self.private_risk={
            SYM:[{
                "symbol":SYM,"positionType":2,"level":1,
                "openType":2,"leverage":1,"maxVol":"1000",
            }],
        }
        self.private_leverage=[
            {"positionType":1,"openType":1,"leverage":2},
            {"positionType":2,"openType":2,"leverage":1},
        ]
        self.calls=[]
        self.fail=False
    async def _private_get(self,path,params=None):
        self.calls.append(("GET",path))
        if self.fail:raise TimeoutError("SECRET_DSN_NOT_FOR_USER")
        mapping={
            "/api/v1/private/position/open_positions":self.positions,
            "/api/v1/private/stoporder/open_orders":self.stops,
            "/api/v1/private/order/open_order_total_count":self.counts,
            "/api/v1/private/account/assets":self.assets,
            "/api/v1/private/position/position_mode":self.mode,
            "/api/v1/private/account/risk_limit":self.private_risk,
            "/api/v1/private/position/leverage":self.private_leverage,
        }
        return deepcopy(mapping[path])


class Response:
    def __init__(self,data):
        self.data=data
    def raise_for_status(self):
        pass
    def json(self):
        return {"success":True,"code":0,"data":deepcopy(self.data)}


class Public:
    def __init__(self):
        self.contract={
            "symbol":SYM,"apiAllowed":True,
            "state":0,"quoteCoin":"USDT","settleCoin":"USDT",
            "futureType":1,"positionOpenType":3,
            "contractSize":"1","volUnit":"1","minVol":"1",
            "maxVol":"1000","priceUnit":"0.01",
        }
        self.depth={
            "bids":[[100,10,1],[99,10,1],[98,10,1]],
            "asks":[[100.1,10,1],[100.2,10,1]],
            "timestamp":1_000_300,"version":12,
        }
        self.calls=[]
        self.fail=False
    async def get(self,path,*,params=None):
        self.calls.append(("GET",path,deepcopy(params)))
        if self.fail:raise TimeoutError("SECRET_URL_NOT_FOR_USER")
        if path==_COUNTRY_PATH:
            assert params=={"symbol":SYM}
            return Response(self.contract)
        if path==_DEPTH_PREFIX+SYM:
            assert params=={"limit":"100"}
            return Response(self.depth)
        raise AssertionError("unexpected public path")


class Clock:
    def __init__(self,*times):
        self.times=list(times)
    def __call__(self):
        return self.times.pop(0)


def setup():
    private=Private()
    public=Public()
    clock=Clock(START,END)
    collector=MexcV40CandidateMarketWitness(
        private=ReadOnlyMexcProbe(private),
        public=public,clock_ms=clock,
    )
    return collector,private,public


def intent(*,notional="500"):
    return EntryIntent.build(
        signal_id=42,symbol=SYM,
        reference_price="100",notional_usdt=notional,
        contract_size="1",volume_step="1",
        min_volume="1",max_volume="1000",
        price_tick=".01",
    )


def run(c, *args, **kwargs):
    return asyncio.run(c.observe(candidate(),*args,**kwargs))


def test_signed_get_bookends_flat_account_and_public_market_without_approval():
    c,private,public=setup()
    out=run(c)
    assert out.status=="V40_GET_ONLY_FLAT_ACCOUNT_AND_MARKET_QUOTED_NONAUTHORIZING"
    assert out.account_equity_usdt==D("5000")
    assert out.account_available_usdt==D("4900")
    assert out.book_age_ms==200
    assert out.bookended_flat_account
    assert out.hedge_mode_observed
    assert out.private_risk_row_observed
    assert out.private_short_cross_1x_settings_bookended
    assert not out.private_risk_limit_settings_fields_missing
    assert out.top_bid==D("100")
    assert out.candidate_contract.contract_size==D("1")
    assert out.candidate_contract.market_state_and_country_verified
    assert out.candidate_contract.private_order_permission_verified is False
    assert out.full_cross_collateral_verified is False
    assert out.complete_realized_daily_loss_verified is False
    assert out.private_key_order_permission_verified is False
    assert not out.entry_allowed
    assert not out.live_authorization
    assert len(private.calls)==14
    assert sum(p=="/api/v1/private/account/risk_limit" for _,p in private.calls)==2
    assert sum(p=="/api/v1/private/position/leverage" for _,p in private.calls)==2
    assert all(x[0]=="GET" for x in private.calls)
    assert all(x[0]=="GET" for x in public.calls)


def test_exact_lot_depth_vwap_and_slippage_are_recomputed_not_assumed():
    c,_,_=setup()
    quote,depth=asyncio.run(c.quote_exact_intent(
        candidate(),intent(notional="1500"),
        max_slippage_bps=D("100"),
    ))
    assert depth.status=="V40_EXECUTABLE_LOT_DISPLAYED_DEPTH_NONAUTHORIZING"
    assert depth.contracts==D("15")
    assert depth.sell_vwap==D("99.66666666666666666666666667")
    assert depth.adverse_slippage_bps<D("100")
    assert depth.order_fits_depth
    assert not depth.entry_allowed
    assert quote.candidate_contract.private_order_permission_verified is False


@pytest.mark.parametrize("fault,expect",[
    ("open_position","MARKET_NOT_FLAT_HEDGE_ACCOUNT"),
    ("pending_order","MARKET_NOT_FLAT_HEDGE_ACCOUNT"),
    ("one_way","MARKET_NOT_FLAT_HEDGE_ACCOUNT"),
    ("bad_asset","MARKET_FLAT_ASSET_ECONOMICS_UNVERIFIED"),
    ("available_above_equity","MARKET_USDT_COLLATERAL_DRIFT"),
    ("offline","MARKET_COUNTRY_CONTRACT_NOT_ELIGIBLE"),
    ("no_public_api","MARKET_COUNTRY_CONTRACT_NOT_ELIGIBLE"),
    ("isolated_only","MARKET_COUNTRY_CONTRACT_NOT_ELIGIBLE"),
    ("private_isolated","MARKET_PRIVATE_RISK_LEVERAGE_CONFLICT"),
    ("private_leverage_two","MARKET_PRIVATE_RISK_LEVERAGE_CONFLICT"),
    ("real_private_isolated","MARKET_PRIVATE_CROSS_ONE_X_UNVERIFIED"),
    ("real_private_leverage_two","MARKET_PRIVATE_CROSS_ONE_X_UNVERIFIED"),
    ("stale_depth","MARKET_BOOK_STALE_AFTER_FINAL_REFRESH"),
    ("unsorted_bids","MARKET_DEPTH_NOT_STRICTLY_SORTED"),
    ("crossed_book","MARKET_CROSSED_ORDER_BOOK"),
])
def test_unverified_exchange_economics_are_rejected(fault,expect):
    c,pub,public=setup()
    if fault=="open_position":pub.positions=[{"symbol":"OTHER_USDT"}]
    if fault=="pending_order":
        pub.counts["sumCount"]=pub.counts["limitOrderCount"]=1
    if fault=="one_way":pub.mode={"positionMode":2}
    if fault=="bad_asset":pub.assets[0]["positionMargin"]="5"
    if fault=="available_above_equity":pub.assets[0]["availableBalance"]="5001"
    if fault=="offline":public.contract["state"]=3
    if fault=="no_public_api":public.contract["apiAllowed"]=False
    if fault=="isolated_only":public.contract["positionOpenType"]=1
    if fault=="private_isolated":pub.private_risk[SYM][0]["openType"]=1
    if fault=="private_leverage_two":pub.private_risk[SYM][0]["leverage"]=2
    if fault=="real_private_isolated":
        pub.private_leverage[1]["openType"]=1
    if fault=="real_private_leverage_two":
        pub.private_leverage[1]["leverage"]=2
    if fault=="stale_depth":
        public.depth["timestamp"]=START-10_000
        c.clock_ms=Clock(START,END,END+100,END+200,END+300)
    if fault=="unsorted_bids":public.depth["bids"]=[[99,10],[100,10]]
    if fault=="crossed_book":public.depth["asks"]=[[99,10],[100,10]]
    with pytest.raises(V40CandidateMarketHalted,match=expect):
        run(c)


def test_insufficient_actual_quantity_fails_even_if_tiny_top_bid_is_good():
    c,_,_=setup()
    with pytest.raises(V40CandidateMarketHalted,match="INSUFFICIENT_DISPLAYED_BIDS"):
        asyncio.run(c.quote_exact_intent(
            candidate(),intent(notional="4000"),
        ))


def test_depth_slippage_limit_is_enforced_for_exact_lot_quantity():
    c,_,_=setup()
    with pytest.raises(V40CandidateMarketHalted,match="DEPTH_SLIPPAGE"):
        asyncio.run(c.quote_exact_intent(
            candidate(),intent(notional="2500"),
            max_slippage_bps=D("10"),
        ))


def test_private_max_contract_cap_applies_to_rounded_lot_quantity():
    c,private,_=setup()
    private.private_risk[SYM][0]["maxVol"]="5"
    with pytest.raises(V40CandidateMarketHalted,match="CONTRACT_OR_RISK_CAP"):
        asyncio.run(c.quote_exact_intent(
            candidate(),intent(notional="600"),
        ))


def test_signed_get_failure_hides_credentials_and_does_not_fallback():
    c,p,_=setup()
    p.fail=True
    with pytest.raises(V40CandidateMarketHalted) as ex:
        run(c)
    assert str(ex.value)=="MARKET_SIGNED_GET_FAILED"
    assert "SECRET" not in str(ex.value)


def test_public_timeout_hides_url_secret_and_fails_closed():
    c,_,pub=setup()
    pub.fail=True
    with pytest.raises(V40CandidateMarketHalted) as ex:
        run(c)
    assert str(ex.value)=="MARKET_PUBLIC_GET_FAILED"
    assert "SECRET" not in str(ex.value)


def test_unknown_mode_or_missing_response_never_defaults_to_hedge():
    p=Private()
    p.mode={"mode":1}
    with pytest.raises(VenueReadHalted,match="POSITION_MODE_RESPONSE_UNVERIFIED"):
        asyncio.run(ReadOnlyMexcProbe(p).raw_position_mode())


def test_invalid_contract_lot_grid_never_rounds_up():
    c,_,pub=setup()
    pub.contract["minVol"]="0.4"
    with pytest.raises(V40CandidateMarketHalted,match="CONTRACT_LOT_GRID"):
        run(c)


def test_insufficient_available_margin_does_not_pass_quantity_quote():
    c,private,_=setup()
    private.assets[0]["availableBalance"]="100"
    with pytest.raises(
        V40CandidateMarketHalted,
        match="ONE_X_MARGIN_WITH_FEE_BUFFER_INSUFFICIENT",
    ):
        asyncio.run(c.quote_exact_intent(candidate(),intent(notional="500")))


def test_risk_preliminary_inspect_cannot_forge_sized_vwap_or_live_permit():
    # Real timestamp is supplied so signal age is within 900 seconds.
    signal=candidate()
    now=signal.admission_open_ts*1000+25_000
    p=Private()
    pub=Public()
    # Public depth and current signed GET frame are timestamped together.
    pub.depth["timestamp"]=now+100
    witness=MexcV40CandidateMarketWitness(
        private=ReadOnlyMexcProbe(p),public=pub,
        clock_ms=Clock(now,now+300,now+500),
    )
    risk,terms=asyncio.run(witness.inspect(signal))
    assert risk.observed_sell_vwap==D(0)
    assert risk.current_account_equity_usdt==D("5000")
    assert risk.cross_collateral_verified is False
    assert risk.private_risk_tier_verified is False
    assert risk.contract_trading_access_verified is False
    assert risk.no_unresolved_mutations is False
    assert terms.private_order_permission_verified is False
    assert risk.inventory_bookended_complete is True


def test_changed_collateral_component_even_with_same_equity_blocks():
    # A bookended account transfer/freeze cannot be treated as stable
    # merely because equity and availableBalance happened not to change.
    p=Private()
    public=Public()

    class DriftingPrivate(Private):
        async def _private_get(self,path,params=None):
            result=await super()._private_get(path,params)
            if path=="/api/v1/private/account/assets":
                self._assets_read=getattr(self,"_assets_read",0)+1
                if self._assets_read==2:
                    result[0]["cashBalance"]="4990"
            return result

    private=DriftingPrivate()
    w=MexcV40CandidateMarketWitness(
        private=ReadOnlyMexcProbe(private),public=public,
        clock_ms=Clock(START,END),
    )
    with pytest.raises(V40CandidateMarketHalted,match="COLLATERAL_COMPONENT_DRIFT"):
        run(w)


def test_documented_mexc_available_above_equity_micro_gap_is_capped_not_spent():
    c,private,_=setup()
    for row in private.assets:
        if row.get("currency")=="USDT":
            row["cashBalance"]="5000"
            row["equity"]="5000"
            row["availableBalance"]="5000.00000000658"
    quote=run(c)
    assert quote.account_equity_usdt==D("5000")
    assert quote.account_available_usdt==D("5000")
    assert quote.candidate_contract.private_order_permission_verified is False
    assert not quote.live_authorization


def test_material_available_above_equity_is_still_rejected():
    c,private,_=setup()
    private.assets[0]["cashBalance"]="5000"
    private.assets[0]["availableBalance"]="5000.000001"
    with pytest.raises(V40CandidateMarketHalted,match="USDT_COLLATERAL_DRIFT"):
        run(c)


def test_micro_available_gap_with_different_cash_equity_is_rejected():
    c,private,_=setup()
    private.assets[0]["cashBalance"]="4999"
    private.assets[0]["availableBalance"]="5000.00000000658"
    with pytest.raises(V40CandidateMarketHalted,match="USDT_COLLATERAL_DRIFT"):
        run(c)


def test_risk_tier_without_optional_margin_and_leverage_fields_uses_dedicated_get():
    c,private,_=setup()
    private.private_risk[SYM][0].pop("openType")
    private.private_risk[SYM][0].pop("leverage")
    out=run(c)
    assert out.status=="V40_GET_ONLY_FLAT_ACCOUNT_AND_MARKET_QUOTED_NONAUTHORIZING"
    assert out.private_short_cross_1x_settings_bookended
    assert out.private_risk_limit_settings_fields_missing
    assert out.private_key_order_permission_verified is False
    assert out.live_authorization is False


def test_risk_limit_fields_conflicting_with_dedicated_signed_get_fail_closed():
    c,private,_=setup()
    private.private_risk[SYM][0]["openType"]=1
    with pytest.raises(V40CandidateMarketHalted,match="RISK_LEVERAGE_CONFLICT"):
        run(c)


@pytest.mark.parametrize("field,value",[
    ("openType",1),("leverage",50),
])
def test_actual_short_isolated_or_50x_is_not_witnessed_as_cross_one_x(field,value):
    c,private,_=setup()
    private.private_leverage[1][field]=value
    # The dedicated GET defines actual configuration. Even if the
    # risk_limit row says CROSS 1x, this can never pass.
    with pytest.raises(V40CandidateMarketHalted,match="CROSS_ONE_X_UNVERIFIED"):
        run(c)


@pytest.mark.parametrize("payload",[
    [],None,{},[{"positionType":1,"openType":2,"leverage":1}],
    [{"positionType":2,"openType":2,"leverage":True}],
    [{"positionType":2,"openType":2,"leverage":0}],
    [{"positionType":2,"openType":2,"leverage":1,"symbol":"WRONG_USDT"}],
    [{"positionType":2,"openType":2,"leverage":1}]*2,
])
def test_missing_or_invalid_dedicated_config_blocks_without_fallback(payload):
    c,private,_=setup()
    private.private_leverage=payload
    with pytest.raises(V40CandidateMarketHalted,match=(
        "MARKET_SIGNED_GET_FAILED|MARKET_PRIVATE_SHORT_SETTINGS_UNVERIFIED"
    )):
        run(c)


def test_private_leverage_change_during_depth_observation_halts():
    class DriftingLeverage(Private):
        async def _private_get(self,path,params=None):
            result=await super()._private_get(path,params)
            if path=="/api/v1/private/position/leverage":
                n=getattr(self,"lever_reads",0)+1
                self.lever_reads=n
                if n==2:
                    result[1]["leverage"]=50
            return result
    private=DriftingLeverage()
    public=Public()
    witness=MexcV40CandidateMarketWitness(
        private=ReadOnlyMexcProbe(private),public=public,
        clock_ms=Clock(START,END),
    )
    with pytest.raises(V40CandidateMarketHalted,match="PRIVATE_CONFIG_BOOKEND_DRIFT"):
        run(witness)


def test_private_risk_two_sides_row_order_only_passes_market_get():
    class ReorderingRisk(Private):
        async def _private_get(self,path,params=None):
            result=await super()._private_get(path,params)
            if path=="/api/v1/private/account/risk_limit":
                n=getattr(self,"risk_reads",0)+1
                self.risk_reads=n
                if n==2:
                    result[SYM].reverse()
            return result

    private=ReorderingRisk()
    private.private_risk[SYM].insert(0,{
        "symbol":SYM,"positionType":1,"level":1,
        "openType":2,"leverage":1,"maxVol":"1000",
    })
    witness=MexcV40CandidateMarketWitness(
        private=ReadOnlyMexcProbe(private),public=Public(),
        clock_ms=Clock(START,END),
    )
    quote=run(witness)
    assert quote.private_risk_row_order_normalized is True
    assert quote.private_short_cross_1x_settings_bookended
    assert quote.private_key_order_permission_verified is False
    assert quote.full_cross_collateral_verified is False
    assert quote.entry_allowed is False
    assert quote.live_authorization is False
    assert all(x[0]=="GET" for x in private.calls)


@pytest.mark.parametrize("change",["maxVol","level","openType","unknown"])
def test_row_reorder_cannot_hide_changed_risk_fields(change):
    class ReorderingAndChangingRisk(Private):
        async def _private_get(self,path,params=None):
            result=await super()._private_get(path,params)
            if path=="/api/v1/private/account/risk_limit":
                n=getattr(self,"risk_reads",0)+1
                self.risk_reads=n
                if n==2:
                    result[SYM].reverse()
                    row=next(r for r in result[SYM] if r["positionType"]==2)
                    if change=="maxVol":
                        row["maxVol"]="999"
                    elif change=="level":
                        row["level"]=2
                    elif change=="openType":
                        row["openType"]=1
                    else:
                        row["undocumentedField"]="redacted"
            return result

    private=ReorderingAndChangingRisk()
    private.private_risk[SYM].insert(0,{
        "symbol":SYM,"positionType":1,"level":1,
        "openType":2,"leverage":1,"maxVol":"1000",
    })
    witness=MexcV40CandidateMarketWitness(
        private=ReadOnlyMexcProbe(private),public=Public(),
        clock_ms=Clock(START,END),
    )
    with pytest.raises(V40CandidateMarketHalted,match="PRIVATE_CONFIG_BOOKEND_DRIFT"):
        run(witness)


def test_risk_side_duplicates_and_bad_identity_cannot_be_canonicalized():
    from app.t100_live_v40_candidate_market_witness import _risk_limit_canonical
    from copy import deepcopy
    original={SYM:[
        {"symbol":SYM,"positionType":1,"level":1,"maxVol":"100"},
        {"symbol":SYM,"positionType":2,"level":1,"maxVol":"100"},
    ]}
    canon=_risk_limit_canonical(original,SYM)
    rev=deepcopy(original)
    rev[SYM].reverse()
    assert _risk_limit_canonical(rev,SYM)==canon
    for mutation in ("duplicate","wrong_symbol","bool_level","missing_side"):
        bad=deepcopy(original)
        if mutation=="duplicate":bad[SYM][1]["positionType"]=1
        if mutation=="wrong_symbol":bad[SYM][1]["symbol"]="OTHER_USDT"
        if mutation=="bool_level":bad[SYM][1]["level"]=True
        if mutation=="missing_side":bad[SYM].clear()
        with pytest.raises(V40CandidateMarketHalted,match="CANONICAL_"):
            _risk_limit_canonical(bad,SYM)


def test_private_risk_maxvol_change_during_depth_observation_halts():
    class DriftingRisk(Private):
        async def _private_get(self,path,params=None):
            result=await super()._private_get(path,params)
            if path=="/api/v1/private/account/risk_limit":
                n=getattr(self,"risk_reads",0)+1
                self.risk_reads=n
                if n==2:
                    result[SYM][0]["maxVol"]="2"
            return result
    private=DriftingRisk()
    public=Public()
    witness=MexcV40CandidateMarketWitness(
        private=ReadOnlyMexcProbe(private),public=public,
        clock_ms=Clock(START,END),
    )
    with pytest.raises(V40CandidateMarketHalted,match="PRIVATE_CONFIG_BOOKEND_DRIFT"):
        run(witness)


def test_private_risk_tier_level_must_be_positive():
    c,private,_=setup()
    private.private_risk[SYM][0]["level"]=0
    with pytest.raises(VenueReadHalted,match="RISK_LIMIT_ROW_UNVERIFIED"):
        asyncio.run(ReadOnlyMexcProbe(private).raw_account_risk_limits(SYM))
    with pytest.raises(V40CandidateMarketHalted,match="MARKET_SIGNED_GET_FAILED"):
        run(c)


def test_duplicate_short_settings_from_venue_do_not_pass():
    c,private,_=setup()
    private.private_leverage=[
        {"positionType":2,"openType":2,"leverage":1},
        {"positionType":2,"openType":2,"leverage":1},
    ]
    with pytest.raises(V40CandidateMarketHalted,match="MARKET_SIGNED_GET_FAILED"):
        run(c)


def test_current_v40_candidate_excludes_btc_and_eth_without_mexc_get():
    from dataclasses import replace
    for sym in ("BTC_USDT","ETH_USDT"):
        c,private,public=setup()
        wrong=replace(candidate(),symbol=sym)
        with pytest.raises(V40CandidateMarketHalted,match="MARKET_SIGNAL_IDENTITY_INVALID"):
            asyncio.run(c.observe(wrong))
        assert private.calls==[]
        assert public.calls==[]


# Regression: the original intermediate book can become old during the
# post-depth signed GET bookends. A later, freshly timestamped public depth
# can be used ONLY if its version/clock advance, the entire signed account
# read remains valid and both original freshness/cycle caps still hold.
class SequencedDepth(Public):
    def __init__(self,*,replacement=None):
        super().__init__()
        self.depth_calls=0
        self.replacement=deepcopy(replacement) if replacement is not None else None
    async def get(self,path,*,params=None):
        if path==_DEPTH_PREFIX+SYM:
            self.depth_calls+=1
            if self.depth_calls==2 and self.replacement is not None:
                self.depth=deepcopy(self.replacement)
        return await super().get(path,params=params)


def refreshed_case(*,old_timestamp=START+100,replacement_timestamp=START+2600,
                   replacement_version=13,end=START+2500,refresh_end=START+2700):
    c,private,old_public=setup()
    public=SequencedDepth()
    public.depth["timestamp"]=old_timestamp
    if replacement_timestamp is not None:
        replacement=deepcopy(public.depth)
        replacement["timestamp"]=replacement_timestamp
        replacement["version"]=replacement_version
        public.replacement=replacement
    c.public=public
    c.clock_ms=Clock(START,end,refresh_end)
    return c,private,public


def test_aged_intermediate_book_is_refreshed_once_after_signed_bookends():
    c,private,public=refreshed_case()
    q=run(c)
    assert q.book_age_ms==100
    assert q.observed_at_ms==START+2700
    assert q.bookended_flat_account
    assert q.private_short_cross_1x_settings_bookended
    assert q.current_private_short_max_vol==D("1000")
    assert public.depth_calls==2
    assert len(private.calls)==14
    assert all(op=="GET" for op,*_ in private.calls)
    assert all(op=="GET" for op,*_ in public.calls)


def test_refreshed_depth_is_used_for_exact_lot_vwap_not_old_snapshot():
    c,private,public=refreshed_case()
    public.replacement["bids"]=[[100,10,1],[99.8,10,1],[99.7,10,1]]
    q,d=asyncio.run(c.quote_exact_intent(
        candidate(),intent(notional="1500"),max_slippage_bps=D("100"),
    ))
    assert public.depth_calls==2
    assert d.age_ms==q.book_age_ms==100
    assert d.order_fits_depth
    assert d.sell_vwap==D("99.93333333333333333333333333")
    assert not q.entry_allowed and not q.live_authorization
    assert not d.entry_allowed and not d.live_authorization


@pytest.mark.parametrize("kind,expected",[
    ("second_still_old","MARKET_BOOK_STALE_AFTER_FINAL_REFRESH"),
    ("version_regresses","MARKET_BOOK_SNAPSHOT_REGRESSED"),
    ("timestamp_regresses","MARKET_BOOK_SNAPSHOT_REGRESSED"),
    ("future_timestamp","MARKET_BOOK_TIMESTAMP_OR_VERSION_STALE"),
    ("bad_version","MARKET_BOOK_TIMESTAMP_OR_VERSION_STALE"),
    ("cycle_expired","MARKET_TOTAL_CYCLE_STALE"),
])
def test_invalid_refresh_never_passes_or_retries_again(kind,expected):
    c,private,public=refreshed_case()
    if kind=="second_still_old":
        public.replacement=None
        c.clock_ms=Clock(START,START+2500,START+2700,START+3100,START+3500)
    elif kind=="version_regresses":
        public.replacement["version"]=11
        c.clock_ms=Clock(START,START+2500,START+2700,START+3100,START+3500)
    elif kind=="timestamp_regresses":
        public.replacement["timestamp"]=START
        c.clock_ms=Clock(START,START+2500,START+2700,START+3100,START+3500)
    elif kind=="future_timestamp":
        public.replacement["timestamp"]=START+5000
    elif kind=="bad_version":
        public.replacement["version"]=0
    elif kind=="cycle_expired":
        c.clock_ms=Clock(START,START+4800,START+5200)
        public.replacement["timestamp"]=START+5100
    with pytest.raises(V40CandidateMarketHalted,match=expected):
        run(c)
    assert public.depth_calls==(
        4 if kind in ("second_still_old","version_regresses",
                      "timestamp_regresses") else 2
    )
    assert len(private.calls)==14


@pytest.mark.parametrize("field",["timestamp","version"])
def test_malformed_initial_depth_metadata_never_triggers_refresh(field):
    c,private,public=refreshed_case()
    public.depth[field]=None
    with pytest.raises(V40CandidateMarketHalted,match="MARKET_BOOK_TIMESTAMP_OR_VERSION_STALE"):
        run(c)
    assert public.depth_calls==1
    assert len(private.calls)==14


def test_account_or_risk_drift_blocks_before_any_final_depth_refresh():
    c,private,public=refreshed_case()
    private.assets[0]["positionMargin"]="4"
    with pytest.raises(V40CandidateMarketHalted,match="MARKET_FLAT_ASSET_ECONOMICS_UNVERIFIED"):
        run(c)
    assert public.depth_calls==1


# ARIA #687 regression: a single last-mile public depth retry remained stale.
# A healthy full exchange snapshot can arrive on the next read; if not,
# fail closed and emit ONE diagnostic only after the retries are exhausted.
def test_stale_second_depth_get_can_be_rescued_by_third_fresh_get(capsys):
    c,private,public=refreshed_case(replacement_timestamp=None)
    fresh=deepcopy(public.depth)
    fresh["timestamp"]=START+2900
    fresh["version"]=13
    original=public.get
    async def delayed_fresh(path,*,params=None):
        if path==_DEPTH_PREFIX+SYM and public.depth_calls==2:
            public.depth=deepcopy(fresh)
        return await original(path,params=params)
    public.get=delayed_fresh
    c.clock_ms=Clock(START,START+2500,START+2700,START+3100)
    q=run(c)
    assert q.book_age_ms==200
    assert q.observed_at_ms==START+3100
    assert q.bookended_flat_account is True
    assert public.depth_calls==3
    assert len(private.calls)==14
    assert all(op=="GET" for op,*_ in private.calls)
    assert capsys.readouterr().out==""


def test_all_three_final_depth_reads_stale_logs_once_only_after_exhaustion(capsys):
    c,private,public=refreshed_case(replacement_timestamp=None)
    c.clock_ms=Clock(START,START+2500,START+2700,START+3100,START+3500)
    with pytest.raises(V40CandidateMarketHalted,match="^MARKET_BOOK_STALE_AFTER_FINAL_REFRESH$"):
        run(c)
    assert public.depth_calls==4
    assert len(private.calls)==14
    lines=capsys.readouterr().out.splitlines()
    assert len(lines)==1
    import json
    msg=json.loads(lines[0])
    assert msg["status"]=="V40_MARKET_DEPTH_FRESHNESS_EXHAUSTED"
    assert msg["symbol"]==SYM
    assert msg["public_depth_gets"]==4
    assert msg["book_version_advanced"] is False
    assert msg["last_book_age_ms"]==3400
    assert msg["freshness_limit_ms"]==1500
    assert msg["elapsed_cycle_ms"]==3500
    assert msg["exchange_posts"]==0


def test_retries_remain_bounded_to_total_cycle_five_seconds(capsys):
    c,private,public=refreshed_case(replacement_timestamp=None)
    c.clock_ms=Clock(START,START+2500,START+2700,START+5200)
    with pytest.raises(V40CandidateMarketHalted,match="^MARKET_TOTAL_CYCLE_STALE$"):
        run(c)
    assert public.depth_calls==3
    assert len(private.calls)==14
    # A total-cycle failure never pretends the refresh was just stale.
    assert "V40_MARKET_DEPTH_FRESHNESS_EXHAUSTED" not in capsys.readouterr().out


def test_bad_refresh_version_stops_without_silent_third_get():
    c,private,public=refreshed_case(replacement_version=0)
    with pytest.raises(V40CandidateMarketHalted,match="^MARKET_BOOK_TIMESTAMP_OR_VERSION_STALE$"):
        run(c)
    assert public.depth_calls==2 and len(private.calls)==14



# BIGTIME #708 (2026-11-27): a public REST book can regress relative to
# the previous read. It must NEVER become a pricing candidate; retry a
# bounded number of times and accept only a newer + age-valid snapshot.
@pytest.mark.parametrize("regression",["timestamp","version"])
def test_regressed_rest_snapshot_can_recover_without_using_bad_book(
    regression,capsys,
):
    c,private,public=refreshed_case()
    if regression=="timestamp":
        public.replacement["timestamp"]=START
    else:
        public.replacement["version"]=11
    repaired=deepcopy(public.depth)
    repaired["timestamp"]=START+3050
    repaired["version"]=14
    repaired["bids"]=[[100,10,1],[99.8,10,1],[99.7,10,1]]
    get_original=public.get
    async def get_with_repair(path,*,params=None):
        if path==_DEPTH_PREFIX+SYM and public.depth_calls==2:
            public.depth=deepcopy(repaired)
        return await get_original(path,params=params)
    public.get=get_with_repair
    c.clock_ms=Clock(START,START+2500,START+2700,START+3200)
    q,exact=asyncio.run(c.quote_exact_intent(
        candidate(),intent(notional="1500"),
        max_slippage_bps=D("100"),
    ))
    assert q.book_age_ms==150
    assert exact.age_ms==150
    assert exact.sell_vwap==D("99.93333333333333333333333333")
    assert public.depth_calls==3
    assert len(private.calls)==14
    assert not q.entry_allowed and not exact.entry_allowed
    assert capsys.readouterr().out==""


@pytest.mark.parametrize("regression",["timestamp","version"])
def test_regressions_exhaust_without_order_or_intermediate_errors(
    regression,capsys,
):
    c,private,public=refreshed_case()
    if regression=="timestamp":
        public.replacement["timestamp"]=START
    else:
        public.replacement["version"]=11
    c.clock_ms=Clock(START,START+2500,START+2700,START+3100,START+3500)
    with pytest.raises(
        V40CandidateMarketHalted,
        match="^MARKET_BOOK_SNAPSHOT_REGRESSED$",
    ):
        run(c)
    assert public.depth_calls==4
    assert len(private.calls)==14
    assert all(op=="GET" for op,*_ in private.calls)
    assert all(op=="GET" for op,*_ in public.calls)
    import json
    notices=capsys.readouterr().out.splitlines()
    assert len(notices)==1
    record=json.loads(notices[0])
    assert record["status"]=="V40_MARKET_DEPTH_SNAPSHOT_REGRESSION_EXHAUSTED"
    assert record["regressed_snapshots"]==3
    assert record["public_depth_gets"]==4
    assert record["exchange_posts"]==0
    # A recent timestamp is NOT sufficient if its version regressed.
    # The metric is the age of the highest timestamp observed, never an
    # authorization to use that snapshot for prices or order submission.
    assert record["last_book_age_ms"]==(
        900 if regression=="version" else 3400
    )


def test_regressed_book_never_becomes_eligible_when_cycle_expires(capsys):
    c,private,public=refreshed_case(replacement_version=11)
    c.clock_ms=Clock(START,START+2500,START+2700,START+5200)
    with pytest.raises(V40CandidateMarketHalted,match="MARKET_TOTAL_CYCLE_STALE"):
        run(c)
    assert public.depth_calls==3
    assert len(private.calls)==14
    # Cycle-expiry is its own immediate, unambiguous failure.
    assert "V40_MARKET_DEPTH_SNAPSHOT_REGRESSION_EXHAUSTED" not in capsys.readouterr().out



# BIGTIME follow-up: a rejected REST snapshot may advance timestamp but
# regress version. A later response with newer version yet older timestamp
# must ALSO be discarded, even though either one field alone looks good.
def test_timestamp_version_high_waters_are_independent_across_rejected_books(capsys):
    c,private,_=setup()
    class Sequence(Public):
        def __init__(self):
            super().__init__()
            self.snapshots=[]
            self.depth_reads=0
        async def get(self,path,*,params=None):
            if path==_DEPTH_PREFIX+SYM:
                self.depth=deepcopy(self.snapshots[self.depth_reads])
                self.depth_reads+=1
            return await super().get(path,params=params)
    p=Sequence()
    initial=deepcopy(p.depth)
    initial["timestamp"]=START+100
    initial["version"]=12
    # Newer timestamp, *lower* version: never a safe price source.
    a=deepcopy(initial)
    a["timestamp"]=START+2700
    a["version"]=11
    # Newer version, but timestamp has regressed relative to rejected a.
    # This is only 550ms old at the second GET and must still be refused.
    b=deepcopy(initial)
    b["timestamp"]=START+2600
    b["version"]=14
    # Finally, both high-water marks advance within the unchanged cap.
    good=deepcopy(initial)
    good["timestamp"]=START+3550
    good["version"]=15
    good["bids"]=[[100,10,1],[99.8,10,1],[99.7,10,1]]
    p.snapshots=[initial,a,b,good]
    c.public=p
    c.clock_ms=Clock(START,START+2500,START+2750,START+3150,START+3650)
    q,exact=asyncio.run(c.quote_exact_intent(
        candidate(),intent(notional="1500"),max_slippage_bps=D("100"),
    ))
    assert p.depth_reads==4
    assert q.book_age_ms==100
    assert exact.age_ms==100
    assert exact.sell_vwap==D("99.93333333333333333333333333")
    assert len(private.calls)==14
    assert all(call[0]=="GET" for call in private.calls)
    assert not q.entry_allowed and not q.live_authorization
    assert capsys.readouterr().out==""

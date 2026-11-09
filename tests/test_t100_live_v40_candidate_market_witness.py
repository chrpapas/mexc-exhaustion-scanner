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
    assert out.top_bid==D("100")
    assert out.candidate_contract.contract_size==D("1")
    assert out.candidate_contract.market_state_and_country_verified
    assert out.candidate_contract.private_order_permission_verified is False
    assert out.full_cross_collateral_verified is False
    assert out.complete_realized_daily_loss_verified is False
    assert out.private_key_order_permission_verified is False
    assert not out.entry_allowed
    assert not out.live_authorization
    assert len(private.calls)==11
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
    ("private_isolated","MARKET_PRIVATE_CROSS_ONE_X_UNVERIFIED"),
    ("private_leverage_two","MARKET_PRIVATE_CROSS_ONE_X_UNVERIFIED"),
    ("stale_depth","MARKET_BOOK_TIMESTAMP_OR_VERSION_STALE"),
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
    if fault=="available_above_equity":pub.assets[0]["availableBalance"]="51"
    if fault=="offline":public.contract["state"]=3
    if fault=="no_public_api":public.contract["apiAllowed"]=False
    if fault=="isolated_only":public.contract["positionOpenType"]=1
    if fault=="private_isolated":pub.private_risk[SYM][0]["openType"]=1
    if fault=="private_leverage_two":pub.private_risk[SYM][0]["leverage"]=2
    if fault=="stale_depth":public.depth["timestamp"]=START-10_000
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

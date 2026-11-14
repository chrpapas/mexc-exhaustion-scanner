"""Offline exact Stage2/lot/depth test harness; absolutely no venue POST/SQL."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from app.t100_live_v40_stage2_source import V40SourceBatch
from app.t100_live_v40_entry_coordinator import V40ContractTerms
from app.t100_live_v40_candidate_market_witness import V40CandidateQuote,V40ExactDepth
from app.t100_v40_render_signal_intake import V40SignalIntakeReport
from app.t100_v40_render_exact_lot_preview import preview_exact_genuine_signal
from tests.test_t100_live_v40_entry_coordinator import candidate
from tests.test_t100_live_v40_stage2_source import T

OBSERVED=T+timedelta(seconds=27)
NOW=int(OBSERVED.timestamp()*1000)
SIGNAL=candidate()


def monitored(**kw):
    d={
        "status":"V40_GENUINE_SIGNAL_AND_FLAT_ACCOUNT_MODELED_NONAUTHORIZING",
        "fresh_source_verified":True,
        "signal_count":1,"signal_id":SIGNAL.signal_id,
        "symbol":SIGNAL.symbol,"tier":SIGNAL.tier,
        "pre24_vol_pct":0.0,
        "current_flat_account_verified":True,
        "account_equity_usdt":"56.37671495",
        "theoretical_v40_notional_usdt":"12.50",
    }
    d.update(kw)
    return V40SignalIntakeReport(**d)


def quote(**kw):
    d={
        "status":"V40_GET_ONLY_FLAT_ACCOUNT_AND_MARKET_QUOTED_NONAUTHORIZING",
        "symbol":SIGNAL.symbol,
        "observed_at_ms":NOW-150,
        "book_age_ms":100,
        "account_equity_usdt":D("56.37671495"),
        "account_available_usdt":D("56.37671495"),
        "candidate_contract":V40ContractTerms(
            contract_size=D(".1"),volume_step=D("1"),
            min_volume=D("1"),max_volume=D("100"),
            price_tick=D(".01"),market_state_and_country_verified=True,
        ),
        "current_private_short_max_vol":D("100"),
        "top_bid":D("100"),"top_ask":D("101"),
        "bookended_flat_account":True,
        "hedge_mode_observed":True,
        "private_risk_row_observed":True,
        "private_short_cross_1x_settings_bookended":True,
    }
    d.update(kw)
    return V40CandidateQuote(**d)


def depth(**kw):
    d={
        "status":"V40_EXECUTABLE_LOT_DISPLAYED_DEPTH_NONAUTHORIZING",
        "symbol":SIGNAL.symbol,"signal_id":SIGNAL.signal_id,
        "contracts":D("1"),"sell_vwap":D("100"),
        "adverse_slippage_bps":D("0"),
        "age_ms":100,"order_fits_depth":True,
    }
    d.update(kw)
    return V40ExactDepth(**d)


class Source:
    def __init__(self, events=None, fail=False):
        self.calls=0
        self.events=[SIGNAL] if events is None else events
        self.fail=fail
    async def latest_candidates(self,*,observed_at):
        self.calls+=1
        assert observed_at==OBSERVED
        if self.fail:
            raise RuntimeError("SECRET SQL")
        return V40SourceBatch(
            "FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING",
            observed_at,candidates=tuple(self.events),
        )


class Market:
    def __init__(self, first=None,second=None,book=None, fail=False):
        self.calls=[]
        self.first=first or quote()
        self.second=second or quote()
        self.book=book or depth()
        self.fail=fail
    async def observe(self,signal):
        self.calls.append("signed_GET_public_GET")
        if self.fail:
            raise RuntimeError("SECRET MEXC HEADER")
        assert signal==SIGNAL
        return self.first
    async def quote_exact_intent(self,signal,intent,*,max_slippage_bps):
        self.calls.append("signed_GET_exact_depth")
        assert signal==SIGNAL
        assert intent.external_oid.startswith("t100v1e")
        assert intent.notional_usdt==D("10")
        assert intent.volume_contracts==D("1")
        assert intent.stop_price==D("175")
        assert max_slippage_bps==D("100")
        return self.second,self.book


def run(source=None,market=None,monitor=None):
    src=source or Source()
    witness=market or Market()
    report=asyncio.run(preview_exact_genuine_signal(
        monitor=monitor or monitored(),
        source=src,market=witness,
        observed_at=OBSERVED,
        clock_ms=lambda:NOW,
    ))
    return report,src,witness


def test_one_verified_fresh_stage2_exact_integer_lot_depth_remains_readonly():
    r,src,market=run()
    assert r.status=="V40_PILOT_REAL_SIGNAL_EXACT_DEPTH_QUOTED_NONAUTHORIZING"
    assert r.signal_id==42 and r.symbol=="EXAMPLE_USDT"
    assert r.capped_target_usdt=="12.50"
    assert D(r.executable_notional_usdt)==D("10")
    assert r.executable_contracts=="1"
    assert r.displayed_sell_vwap=="100"
    assert r.exact_lot_depth_verified is True
    assert r.native_market_stop_verified is False
    assert r.cross_collateral_certified is False
    assert r.exchange_order_permission_certified is False
    assert r.live_authorization is False and r.entry_allowed is False
    assert r.mexc_posts==r.database_writes==0
    assert src.calls==1
    assert market.calls==["signed_GET_public_GET","signed_GET_exact_depth"]


@pytest.mark.parametrize("changes",[
    {"status":"V40_SIGNAL_INTAKE_VOL30_SKIP_REAL_SOURCE"},
    {"symbol":"INVALID"},
    {"signal_count":2},
    {"signal_id":True},
    {"symbol":"BTC_USDT"},
    {"theoretical_v40_notional_usdt":"NaN"},
    {"fresh_source_verified":False},
    {"current_flat_account_verified":False},
    {"entry_allowed":True},
])
def test_ineligible_monitor_is_rejected_before_any_source_or_exchange_GET(changes):
    src,market=Source(),Market()
    r,_,_=run(source=src,market=market,monitor=monitored(**changes))
    assert r.status=="V40_EXACT_PREVIEW_MONITOR_CANDIDATE_INVALID"
    assert src.calls==0 and market.calls==[]


def test_second_restricted_source_must_match_exact_real_stage2():
    for events in ([],[SIGNAL,SIGNAL],[replace(SIGNAL,signal_id=44)],
                   [replace(SIGNAL,symbol="CHANGED_USDT")]):
        src,market=Source(events),Market()
        r,_,_=run(source=src,market=market)
        assert r.status in (
            "V40_EXACT_PREVIEW_SOURCE_NOT_UNIQUE_OR_VERIFIED",
            "V40_EXACT_PREVIEW_MONITORED_SIGNAL_CHANGED",
        )
        assert market.calls==[]


@pytest.mark.parametrize("fault,expected",[
    ({"account_equity_usdt":D("56.40")},
     "V40_EXACT_PREVIEW_ACCOUNT_OR_CROSS_CONFIGURATION_DRIFT"),
    ({"bookended_flat_account":False},
     "V40_EXACT_PREVIEW_ACCOUNT_OR_CROSS_CONFIGURATION_DRIFT"),
    ({"private_short_cross_1x_settings_bookended":False},
     "V40_EXACT_PREVIEW_ACCOUNT_OR_CROSS_CONFIGURATION_DRIFT"),
    ({"full_cross_collateral_verified":True},
     "V40_EXACT_PREVIEW_ACCOUNT_OR_CROSS_CONFIGURATION_DRIFT"),
    ({"current_private_short_max_vol":D(".5")},
     "V40_EXACT_PREVIEW_PRIVATE_SHORT_TIER_EXCEEDED"),
    ({"candidate_contract":V40ContractTerms(
        contract_size=D("1"),volume_step=D("1"),
        min_volume=D("1"),max_volume=D("100"),
        price_tick=D(".01"),market_state_and_country_verified=True,
    )},"V40_EXACT_PREVIEW_LOT_OR_MODELED_STOP_LIMIT_REJECTED"),
])
def test_first_contract_collateral_lot_rejection_avoids_exact_depth(fault,expected):
    market=Market(first=quote(**fault))
    r,_,_=run(market=market)
    assert r.status==expected
    assert market.calls==["signed_GET_public_GET"]
    assert r.entry_allowed is False


@pytest.mark.parametrize("fault",[
    {"account_available_usdt":D("5")},
    {"observed_at_ms":NOW-3000},
    {"candidate_contract":V40ContractTerms(
        contract_size=D(".2"),volume_step=D("1"),
        min_volume=D("1"),max_volume=D("100"),
        price_tick=D(".01"),market_state_and_country_verified=True,
    )},
    {"private_short_cross_1x_settings_bookended":False},
])
def test_second_market_bookend_drift_denies_preview(fault):
    r,_,market=run(market=Market(second=quote(**fault)))
    assert r.status=="V40_EXACT_PREVIEW_CURRENT_DEPTH_OR_COLLATERAL_UNVERIFIED"
    assert len(market.calls)==2


@pytest.mark.parametrize("fault",[
    {"age_ms":1700},
    {"contracts":D("2")},
    {"signal_id":99},
    {"order_fits_depth":False},
    {"adverse_slippage_bps":D("101")},
])
def test_insufficient_stale_or_wrong_order_depth_denied(fault):
    r,_,_=run(market=Market(book=depth(**fault)))
    assert r.status=="V40_EXACT_PREVIEW_CURRENT_DEPTH_OR_COLLATERAL_UNVERIFIED"
    assert r.mexc_posts==0 and r.entry_allowed is False


def test_source_and_venue_exceptions_are_redacted_no_post():
    r,src,market=run(source=Source(fail=True))
    assert r.status=="V40_EXACT_PREVIEW_RESTRICTED_SOURCE_UNAVAILABLE"
    assert market.calls==[]
    r,src,market=run(market=Market(fail=True))
    assert r.status=="V40_EXACT_PREVIEW_MARKET_GET_OR_LOT_INVALID"
    assert "SECRET" not in str(r)
    assert r.database_writes==r.mexc_posts==0

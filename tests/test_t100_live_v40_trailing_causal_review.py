"""Causal completed-bar V40 trailing preview and fail-closed stop preflight."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal as D

import pytest

from app.t100_live_v40_trailing_causal_review import (
    V40TrailingReviewHalted, review_v40_trailing_from_completed_candles,
)
from app.t100_live_v40_trailing_low_source import V40CausalLow
from tests.test_t100_live_trailing_preflight import (
    NOW, book, checkpoint,
)

UTC=timezone.utc
CP=checkpoint(symbol="SIREN_USDT")
CLOCK_FLOOR=NOW//1000//1800*1800
FILL=datetime.fromtimestamp(CLOCK_FLOOR-1810,UTC)
OBS=datetime.fromtimestamp(NOW/1000,UTC)
EXPECTED_FIRST=CLOCK_FLOOR-1800


def evidence(low=D("86"), **kw):
    x=V40CausalLow(
        status="V40_COMPLETED_MIN30_CAUSAL_LOW_NONAUTHORIZING",
        symbol=CP.symbol,
        first_completed_open_ts=EXPECTED_FIRST,
        last_completed_open_ts=EXPECTED_FIRST,
        complete_bars=1,
        observed_low=low,
    )
    return replace(x,**kw)


class Read:
    def __init__(self,value=None,*,error=False):
        self.value=evidence() if value is None else value
        self.error=error
        self.calls=[]
    async def read(self,*,symbol,filled_at,observed_at):
        self.calls.append((symbol,filled_at,observed_at))
        if self.error:raise RuntimeError("SECRET_CONNECTION_PASSWORD")
        return self.value


def invoke(reader=None, *, cp=CP, first=None, second=None,
           market=D("86.1"), filled_at=FILL, observed_at=OBS,
           now_ms=NOW):
    checked=reader if reader is not None else Read()
    first=first if first is not None else book([cp])
    second=second if second is not None else replace(
        first,observed_at_ms=NOW-1000,
    )
    return asyncio.run(review_v40_trailing_from_completed_candles(
        reader=checked,checkpoints=[cp],
        target_position_id=cp.position_id,
        first=first,second=second,
        filled_at=filled_at,observed_at=observed_at,
        current_last_price=market,price_tick=D(".01"),
        market_observed_at_ms=NOW-500,now_ms=now_ms,
    ))


def test_readonly_completed_min30_history_builds_frozen_14pct_stop_proposal():
    source=Read()
    result=invoke(source)
    assert len(source.calls)==1
    assert source.calls[0][0]==CP.symbol
    assert result.status=="V40_TRAILING_COMPLETED_CANDLE_PROPOSAL_ONLY_NOT_AMENDABLE"
    assert result.proposed_stop_price==D("86.5")
    assert result.observed_minimum_price==D("86")
    assert result.completed_causal_bars==1
    assert result.last_completed_bar_open_ts==EXPECTED_FIRST
    assert not result.exchange_signed_fill_time_verified
    assert not result.full_post_fill_minimum_coverage_verified
    assert not result.independently_verified_market_low
    assert not result.exchange_stop_mutation_allowed
    assert not result.entry_allowed
    assert not result.live_authorization


def test_no_favorable_complete_bar_never_invents_a_stop():
    x=V40CausalLow(
        status="V40_NO_CAUSAL_COMPLETED_BARS_NONAUTHORIZING",
        symbol=CP.symbol,
        first_completed_open_ts=EXPECTED_FIRST,
        last_completed_open_ts=EXPECTED_FIRST-1800,
        complete_bars=0,observed_low=None,
    )
    # A position opened seconds ago cannot have a completed post-fill
    # half-hour candle. This must not be confused with missing older data.
    res=invoke(Read(x),filled_at=OBS-timedelta(seconds=20))
    assert res.proposed_stop_price is None
    assert res.status=="V40_TRAILING_NO_COMPLETE_POST_FILL_CANDLES_NONAUTHORIZING"

    with pytest.raises(V40TrailingReviewHalted,match="EXPECTED_BARS_MISSING"):
        invoke(Read(x),filled_at=FILL)


@pytest.mark.parametrize("fault",[
    {"first_completed_open_ts":EXPECTED_FIRST-1800},
    {"last_completed_open_ts":EXPECTED_FIRST+1800},
    {"complete_bars":2},
])
def test_off_by_one_and_incomplete_post_fill_candle_history_halts(fault):
    with pytest.raises(V40TrailingReviewHalted,match="WINDOW_DRIFT"):
        invoke(Read(evidence(**fault)))


def test_no_causal_full_since_entry_coverage_is_never_upgraded():
    with pytest.raises(V40TrailingReviewHalted,match="EVIDENCE_INVALID"):
        invoke(Read(evidence(covers_since_entry_fill=True)))


def test_unsigned_fill_provenance_cannot_be_claimed_by_reader():
    with pytest.raises(V40TrailingReviewHalted,match="EVIDENCE_INVALID"):
        invoke(Read(evidence(signed_fill_provenance_verified=True)))


def test_raw_sql_error_cannot_expose_credentials():
    with pytest.raises(V40TrailingReviewHalted) as e:
        invoke(Read(error=True))
    assert str(e.value)=="V40_TRAILING_CAUSAL_HISTORY_READ_FAILED"
    assert "SECRET" not in str(e.value)


def test_existing_native_stop_missing_prevents_trailing_preview():
    first=book([CP])
    first.stops[0]["stopLossType"]=1
    second=replace(first,observed_at_ms=NOW-1000)
    res=invoke(first=first,second=second)
    assert res.status.startswith(
        "V40_TRAILING_PREFLIGHT_BLOCKED_TRAILING_FIRST_NATIVE_STOP_UNVERIFIED"
    )
    assert res.proposed_stop_price is None
    assert not res.exchange_stop_mutation_allowed


def test_market_rebound_above_target_prevents_amendment_proposal():
    res=invoke(market=D("100"))
    assert res.status=="V40_TRAILING_PREFLIGHT_BLOCKED_TRAILING_PROPOSED_STOP_ALREADY_CROSSED"
    assert res.proposed_stop_price is None


def test_stale_market_and_bookend_preflight_remains_non_authorizing():
    f=book([CP],timestamp=NOW-11000)
    s=replace(f,observed_at_ms=NOW-1000)
    res=invoke(first=f,second=s)
    assert res.status=="V40_TRAILING_PREFLIGHT_BLOCKED_TRAILING_BOOKEND_OR_MARKET_STALE"


def test_fake_fill_in_future_rejected_before_db_reader():
    reader=Read()
    with pytest.raises(V40TrailingReviewHalted,match="CLOCK_OR_CUTOFF"):
        invoke(reader,filled_at=OBS+timedelta(minutes=1))
    assert reader.calls==[]


def test_reader_returns_different_coin_rejected():
    with pytest.raises(V40TrailingReviewHalted,match="SOURCE_INVALID"):
        invoke(Read(evidence(symbol="OTHER_USDT")))


def test_conservative_history_does_not_arm_without_14pct_completed_low():
    res=invoke(Read(evidence(low=D("90"))),market=D("91"))
    assert res.status=="V40_TRAILING_PREFLIGHT_BLOCKED_TRAILING_NO_STOP_UPDATE_DUE"
    assert res.proposed_stop_price is None

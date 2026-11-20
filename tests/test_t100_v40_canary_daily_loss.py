"""Offline V40 signed GET daily-loss evidence: pagination, drift, PnL, redaction."""
import asyncio
from datetime import datetime,timezone
from decimal import Decimal as D

import httpx
import pytest

from app.t100_v40_canary_daily_loss import (
    V40DailyLossHalted,V40HistoryGetOnly,observe_daily_loss,MAX_PAGES,SIZE,PATH,
)

NOW=datetime(2026,11,13,12,0,0,tzinfo=timezone.utc)
MORNING=int(datetime(2026,11,13,9,0,tzinfo=timezone.utc).timestamp()*1000)
YESTERDAY=int(datetime(2026,11,12,23,0,tzinfo=timezone.utc).timestamp()*1000)


def item(identifier=1,ts=MORNING,realised="-1.5",fee="0.2",hold="0"):
    return {
        "positionId":identifier,"state":3,"updateTime":ts,
        "realised":realised,"totalFee":fee,"holdFee":hold,
        "symbol":"BTC_USDT",
    }


class Pages:
    def __init__(self,pages,second=None,crash=False):
        self.pages=pages
        self.second=second if second is not None else pages
        self.calls=[]
        self.crash=crash
    async def page(self,n):
        self.calls.append(n)
        if self.crash:raise OSError("PRIVATE_SECRET_DO_NOT_LOG")
        target=self.pages if len(self.calls)<=len(self.pages) else self.second
        return target[n-1]


def run(reader):
    async def pause(t):
        assert t==1.
    return asyncio.run(observe_daily_loss(reader,now=NOW,pause=pause))


def test_signed_history_twice_conservatively_counts_losses_and_all_fees():
    r=Pages([[item(1,realised="-2",fee="0.3",hold="-0.1"),
              item(2,ts=YESTERDAY,realised="-8",fee="2"),
              item(3,realised="6",fee="0.15")]])
    result=run(r)
    assert result.status=="V40_DAILY_LOSS_TWO_COMPLETE_SIGNED_GET_PASSES_NONAUTHORIZING"
    assert D(result.realized_today_loss_usdt)==D("2.55")
    assert result.closed_positions_today==2
    assert result.total_historical_positions==3
    assert r.calls==[1,1]
    assert result.complete_history_verified and result.complete_two_passes
    assert result.safely_below_12_50
    assert not result.entry_allowed and not result.live_authorization
    assert result.mexc_posts==result.database_writes==0


def test_no_historical_closes_is_verified_zero_not_faked():
    result=run(Pages([[]]))
    assert result.realized_today_loss_usdt=="0"
    assert result.complete_history_verified
    assert result.closed_positions_today==0


def test_full_first_page_requires_second_page_and_no_silent_truncation():
    page=[item(i) for i in range(1,101)]
    r=Pages([page,[item(101)]])
    result=run(r)
    assert result.complete_history_verified
    assert result.total_historical_positions==101
    assert r.calls==[1,2,1,2]


def test_explicit_pagination_metadata_is_checked_and_never_assumed():
    r=Pages([[item(1)]])
    r.pages=[{"pageSize":SIZE,"totalCount":1,"totalPage":1,
              "currentPage":1,"resultList":[item(1)]}]
    r.second=r.pages
    assert run(r).complete_history_verified
    r=Pages([[item(1)]])
    r.pages=[{"pageSize":SIZE,"totalCount":2,"totalPage":1,
              "currentPage":1,"resultList":[item(1)]}]
    r.second=r.pages
    assert run(r).status=="V40_DAILY_LOSS_HISTORY_TRUNCATED"


def test_moving_account_history_never_claims_complete_evidence():
    r=Pages([[item(1)]],second=[[item(1,realised="8")]])
    result=run(r)
    assert result.status=="V40_DAILY_LOSS_HISTORY_CHANGED_BETWEEN_PASSES"
    assert not result.complete_history_verified


@pytest.mark.parametrize("pages,code",[
    ([[item(1),item(1)]],"V40_DAILY_LOSS_HISTORY_IDENTITY_INVALID"),
    ([[item(1,realised="NaN")]],"V40_DAILY_LOSS_NUMERIC_FIELD_INVALID"),
    ([[item(1,ts=MORNING+86400000)]],"V40_DAILY_LOSS_FUTURE_HISTORY_TIMESTAMP"),
    ([[{"state":3}]],"V40_DAILY_LOSS_HISTORY_IDENTITY_INVALID"),
    ([[item(1,realised="0",fee="13")]],"V40_DAILY_LOSS_TWO_COMPLETE_SIGNED_GET_PASSES_NONAUTHORIZING"),
])
def test_malformed_or_unsafe_history_fails_closed(pages,code):
    result=run(Pages(pages))
    assert result.status==code
    if code.endswith("NONAUTHORIZING"):
        assert not result.safely_below_12_50
    else:
        assert not result.complete_history_verified
    assert not result.entry_allowed


def test_overfull_history_halts_instead_of_pretending_older_losses_absent():
    pages=[[item(i+j*SIZE+1) for i in range(SIZE)] for j in range(MAX_PAGES)]
    r=Pages(pages)
    result=run(r)
    assert result.status=="V40_DAILY_LOSS_HISTORY_TRUNCATED"
    assert r.calls==[1,2,3,4]


def test_signed_get_exception_does_not_expose_secret():
    result=run(Pages([[]],crash=True))
    assert result.status=="V40_DAILY_LOSS_SIGNED_HISTORY_UNAVAILABLE"
    assert "PRIVATE_SECRET" not in repr(result)
    assert not result.complete_history_verified


def test_wire_transport_has_only_history_get_with_bounded_pages():
    events=[]
    def mock(request):
        events.append(request)
        return httpx.Response(
            200,json={"success":True,"code":0,"data":[]},
        )
    async def task():
        async with httpx.AsyncClient(
            base_url="https://api.mexc.com",
            transport=httpx.MockTransport(mock),
        ) as http:
            reader=V40HistoryGetOnly(
                http,api_key="DEMO_KEY_1234567890",
                api_secret="DEMO_SECRET_123456789012345",
            )
            raw=await reader.page(1)
            assert raw==[]
            with pytest.raises(V40DailyLossHalted):
                await reader.page(MAX_PAGES+1)
            with pytest.raises(V40DailyLossHalted):
                await reader._private_get(PATH,{"page_num":1,"page_size":500})
            assert not hasattr(reader,"_private_post")
    asyncio.run(task())
    assert len(events)==1
    assert events[0].method=="GET"
    assert events[0].url.path==PATH
    assert events[0].url.params["page_num"]=="1"
    assert events[0].url.params["page_size"]==str(SIZE)

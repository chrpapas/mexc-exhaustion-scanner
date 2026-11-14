"""Test actual scanner -> restricted source -> V40 economics -> GET-only account path."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime,timezone
from decimal import Decimal
import pytest

from app.t100_200_strategy import STRATEGY_ID
from app.t100_live_v40_stage2_source import V40SourceBatch,V40SourceCandidate
from app.t100_v40_admission import PriorBar
from app.t100_v40_render_signal_intake import (
    V40SignalIntakeReport, eligible_environment, inspect_v40_signal_intake,
)
from scripts.t100_v40_pilot_get_only import PilotGetOnlyResult

NOW=datetime(2026,11,11,19,0,tzinfo=timezone.utc)
ORIGIN=int(NOW.timestamp())
KEY="TEST_MEXC_KEY_ISOLATED_123456"
SECRET="TEST_MEXC_SECRET_ISOLATED_789012345"
ENV={
    "T100_V40_EXECUTOR_DISABLED_WORKER":"true",
    "T100_V40_EXECUTOR_KEY_PREFLIGHT_ENABLED":"true",
    "T100_V40_ACCOUNT_PROFILE":"DEDICATED_WEB_PILOT",
    "T100_EXECUTION_MODE":"paper",
    "T100_LIVE_ORDER_API_ENABLED":"false",
    "T100_LIVE_KILL_SWITCH":"true",
    "EXECUTION_ENABLED":"false",
    "TRADING_MODE":"paper",
    "T100_V40_SIGNAL_READER_DSN":"postgresql://fake:SECRET@localhost/mexc_exhaustion",
    "MEXC_API_KEY":KEY,
    "MEXC_API_SECRET":SECRET,
}

def candidate(vol=0.0):
    return V40SourceCandidate(
        signal_id=42,strategy_id=STRATEGY_ID,symbol="TEST_USDT",
        admission_open_ts=ORIGIN,feature_open_ts=ORIGIN-1800,
        entry_reference_price=100.0,stage1_atr7_pct=5.8,
        stage1_return_24h=.20,physical_episode_seq=3,tier="HIGH_225",
        prior_bars=tuple(
            PriorBar(ORIGIN-1800*i,102.0,100.0,100000.0)
            for i in range(49,0,-1)
        ),pre24_vol_pct=vol,observed_age_seconds=35.,
    )

def batch(items):
    return V40SourceBatch(
        status="FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING",
        observed_at=NOW,candidates=tuple(items),
    )

def flat():
    return PilotGetOnlyResult(
        status="PILOT_FLAT_HEDGE_SIGNED_GET_BOOKENDS_NONAUTHORIZING",
        samples=2,position_count=0,native_stop_count=0,
        account_equity_usdt="56.37671495",
        available_balance_usdt="56.37671495",
        hedge_mode_observed=True,inventory_bookends_equal=True,
    )

class Source:
    def __init__(self,result):
        self.result=result
        self.calls=[]
    async def latest_candidates(self,*,observed_at):
        self.calls.append(observed_at)
        return self.result

def run(env=ENV,items=None,report=None):
    src=Source(batch(items if items is not None else [candidate()]))
    calls=[]
    async def observe(*,key,secret):
        calls.append((key,secret))
        return flat() if report is None else report
    out=asyncio.run(inspect_v40_signal_intake(
        env,source=src,observed_at=NOW,observe=observe,
    ))
    return out,src.calls,calls


def test_genuine_candidate_and_real_get_bookends_are_no_order():
    result,reads,gets=run()
    assert reads==[NOW] and gets==[(KEY,SECRET)]
    assert result.status=="V40_GENUINE_SIGNAL_AND_FLAT_ACCOUNT_MODELED_NONAUTHORIZING"
    assert result.fresh_source_verified and result.signal_id==42
    assert result.symbol=="TEST_USDT" and result.tier=="HIGH_225"
    assert result.current_flat_account_verified
    assert result.account_equity_usdt=="56.37671495"
    assert result.theoretical_v40_notional_usdt=="12.50"
    assert result.mexc_posts==result.database_writes==0
    assert not result.entry_allowed and not result.live_authorization
    assert not result.native_stop_execution_verified
    assert not result.durable_entry_claim_verified
    assert not result.native_trailing_exit_verified
    assert not result.independent_nonflat_watchdog_verified
    assert KEY not in str(result) and SECRET not in str(result)
    assert "SECRET@" not in str(result)


def test_empty_source_has_no_private_exchange_get():
    result,reads,gets=run(items=[])
    assert reads==[NOW] and gets==[]
    assert result.status=="V40_SIGNAL_INTAKE_FRESH_EMPTY_SOURCE_OBSERVED"
    assert result.signal_count==0 and result.fresh_source_verified


def test_multi_signal_requires_supervised_review_does_not_force_order():
    result,reads,gets=run(items=[candidate(),replace(candidate(),signal_id=43)])
    assert result.status=="V40_SIGNAL_INTAKE_MULTIPLE_CANDIDATES_SUPERVISED_REVIEW"
    assert result.signal_count==2 and gets==[]
    assert not result.entry_allowed


def test_vol30_skip_blocks_real_account_and_order():
    result,reads,gets=run(items=[candidate(35.0)])
    assert result.status=="V40_SIGNAL_INTAKE_VOL30_SKIP_REAL_SOURCE"
    assert result.pre24_vol_pct==35.0 and gets==[]


@pytest.mark.parametrize("field,value",[
    ("T100_V40_EXECUTOR_DISABLED_WORKER","false"),
    ("T100_V40_EXECUTOR_KEY_PREFLIGHT_ENABLED","false"),
    ("T100_V40_ACCOUNT_PROFILE","SOME_OTHER"),
    ("T100_EXECUTION_MODE","live"),
    ("T100_LIVE_ORDER_API_ENABLED","true"),
    ("T100_LIVE_KILL_SWITCH","false"),
    ("EXECUTION_ENABLED","true"),
    ("TRADING_MODE","live"),
    ("T100_V40_SIGNAL_READER_DSN",""),
    ("DATABASE_URL","postgres://owner:BAD_SECRET@localhost/db"),
    ("T100_LIVE_DATABASE_URL","postgres://owner:BAD_SECRET@localhost/db"),
    ("T100_V40_TRADER_API_KEY","AMBIGUOUS_SECOND_ACCOUNT"),
])
def test_bad_environment_is_no_network_no_db(field,value):
    result,reads,gets=run({**ENV,field:value})
    assert result.status=="V40_SIGNAL_INTAKE_PAPER_KILL_OR_READER_DSN_REQUIRED"
    assert reads==gets==[]


def test_account_unverified_refuses_modeled_size():
    result,reads,gets=run(report=replace(flat(),position_count=1))
    assert result.status=="V40_SIGNAL_INTAKE_NONFLAT_OR_UNVERIFIED_NO_ENTRY"
    assert result.theoretical_v40_notional_usdt is None
    assert not result.current_flat_account_verified
    assert not result.entry_allowed


def test_missing_keys_no_private_get():
    result,reads,gets=run({**ENV,"MEXC_API_SECRET":""})
    assert result.status=="V40_SIGNAL_INTAKE_ACCOUNT_KEYS_MISSING"
    assert result.signal_id==42 and gets==[]


def test_wrong_signal_meta_no_fabricated_entry():
    result,reads,gets=run(items=[replace(candidate(),entry_allowed=True)])
    assert result.status=="V40_SIGNAL_INTAKE_CANDIDATE_PROVENANCE_HALT"
    assert gets==[]


def test_fail_closed_untrusted_source_report():
    result,reads,gets=run()
    assert eligible_environment(ENV)
    async def observe(*,key,secret):
        raise ValueError("SECRET_PRIVATE_MESSAGE")
    report=asyncio.run(inspect_v40_signal_intake(
        ENV,source=Source(batch([candidate()])),observed_at=NOW,observe=observe,
    ))
    assert report.status=="V40_SIGNAL_INTAKE_SIGNED_ACCOUNT_GET_FAILED"
    assert "SECRET_PRIVATE_MESSAGE" not in str(report)
    assert not report.live_authorization

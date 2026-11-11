"""Historical causal signals: real SELECT-contract fixtures, never tradable."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime,timedelta,timezone

import pytest

from app.t100_live_v40_historical_stage2_audit import (
    HISTORICAL_SQL,HISTORY_SQL,MAX_HISTORICAL_RECORDS,
    MAX_LOOKBACK_DAYS,inspect_historical_stage2,
)
from app.t100_live_v40_stage2_source import PostgresV40Stage2Source
from tests.test_t100_live_v40_stage2_source import (
    FakeConn,Pool,T,fixtures,
)

NOW=T+timedelta(days=2)


class HistoricalConn(FakeConn):
    def __init__(self,rows=None,bars=None):
        signal,history=fixtures()
        super().__init__(
            signals=[signal] if rows is None else rows,
            history=history if bars is None else bars,
            actual_role="mexc_scanner",
        )
    async def fetch(self,sql,*args):
        if sql==HISTORICAL_SQL:
            self.queries.append(sql)
            assert args[0]==NOW-timedelta(days=7)
            assert args[1]==NOW
            assert 1<=args[2]<=MAX_HISTORICAL_RECORDS
            return self.signals
        return await super().fetch(sql,*args)


def run(conn,**kwargs):
    return asyncio.run(inspect_historical_stage2(
        PostgresV40Stage2Source(
            Pool(conn),manual_scanner_owner_readonly=True,
        ),observed_at=NOW,**kwargs,
    ))


def test_historical_event_reconstructed_with_frozen_live_causal_49_bars():
    conn=HistoricalConn()
    report=run(conn)
    assert report.status=="V40_HISTORICAL_STAGE2_CAUSAL_SAMPLE_VALID_NONAUTHORIZING"
    assert report.checked_records==1
    assert report.reconstructed_records==1
    assert report.invalid_records==0
    assert report.historical_sample_only is True
    assert report.fresh_signal_discovery_verified is False
    assert report.scanner_uptime_verified is False
    assert report.market_settings_verified is False
    assert report.order_execution_verified is False
    assert report.entry_allowed is False and report.live_authorization is False
    assert report.database_writes==report.mexc_posts==0
    assert report.read_only is True
    record=report.records[0]
    assert record.status=="HISTORICAL_CAUSAL_49_BAR_RECONSTRUCTED"
    assert record.symbol=="EXAMPLE_USDT"
    assert record.signal_id==42
    assert record.tier=="HIGH_225"
    assert record.historical_only is True
    assert record.entry_allowed is False
    assert record.live_authorization is False
    assert HISTORICAL_SQL in conn.queries
    assert HISTORY_SQL in conn.queries
    assert not any(
        sql.lstrip().upper().startswith((
            "INSERT","UPDATE","DELETE","CREATE","ALTER","DROP","GRANT",
        )) for sql in conn.queries
    )


def test_historical_none_does_not_forge_candidate():
    conn=HistoricalConn(rows=[])
    report=run(conn)
    assert report.status=="V40_HISTORICAL_STAGE2_SAMPLE_EMPTY_NONAUTHORIZING"
    assert report.checked_records==0
    assert report.records==()
    assert HISTORY_SQL not in conn.queries


def test_historical_candle_gap_recorded_as_invalid_not_upgraded():
    signal,bars=fixtures()
    conn=HistoricalConn(bars=bars[:-1])
    report=run(conn)
    assert report.status=="V40_HISTORICAL_STAGE2_CAUSAL_SAMPLE_GAPS_NONAUTHORIZING"
    assert report.invalid_records==1
    assert report.records[0].status=="SOURCE_CAUSAL_49_BAR_COUNT_INVALID"
    assert report.records[0].causal_49_bars_reconstructed is False


def test_late_created_paper_signal_fails_asof_15minute_historical_clock():
    signal,bars=fixtures()
    signal["created_at"]=signal["p2_at"]+timedelta(minutes=16)
    report=run(HistoricalConn(rows=[signal]))
    assert report.invalid_records==1
    assert report.records[0].status=="SOURCE_SIGNAL_CLOCK_OR_AGE_INVALID"
    assert report.entry_allowed is False


def test_old_paper_trade_decision_is_ignored_for_replay_but_never_traded():
    signal,_=fixtures()
    signal["trade_decision"]="accepted"
    report=run(HistoricalConn(rows=[signal]))
    assert report.reconstructed_records==1
    assert not report.fresh_signal_discovery_verified
    assert not report.entry_allowed


def test_wrong_scanner_tier_is_rejected():
    signal,_=fixtures()
    signal["t100_tier"]="LOW_100"
    report=run(HistoricalConn(rows=[signal]))
    assert report.invalid_records==1
    assert report.records[0].status=="SOURCE_STAGE2_CONTRACT_OR_TIER_MISMATCH"


def test_duplicate_historical_signal_id_halts_without_partial_success():
    signal,_=fixtures()
    conn=HistoricalConn(rows=[signal,deepcopy(signal)])
    report=run(conn)
    assert report.status=="V40_HISTORICAL_IDENTITY_OR_WINDOW_INVALID_HALT"
    assert report.records==()
    assert report.reconstructed_records==0


def test_future_signal_and_wrong_window_halt_before_candle_queries():
    signal,_=fixtures()
    signal["p2_at"]=NOW+timedelta(minutes=1)
    conn=HistoricalConn(rows=[signal])
    report=run(conn)
    assert report.status=="V40_HISTORICAL_IDENTITY_OR_WINDOW_INVALID_HALT"
    assert HISTORY_SQL not in conn.queries


@pytest.mark.parametrize("role",["t100_live_executor_runtime","t100_shadow_witness_writer",
                                  "t100_shadow_sentinel_outbox","random_role"])
def test_owner_role_is_required_even_under_readonly_transaction(role):
    conn=HistoricalConn()
    conn.actual_role=role
    report=run(conn)
    assert report.status=="V40_HISTORICAL_READONLY_OWNER_IDENTITY_HALT"
    assert HISTORICAL_SQL not in conn.queries


@pytest.mark.parametrize("flag,value",[
    ("readonly_role",False),
])
def test_not_an_arbitrary_owner_role_override(flag,value):
    conn=HistoricalConn()
    setattr(conn,flag,value)
    report=run(conn)
    # Explicit actual scanner role, not an overridable role label.
    assert report.historical_sample_only is True


def test_readonly_contract_is_independent_of_live_executor_source():
    conn=HistoricalConn()
    normal_source=PostgresV40Stage2Source(Pool(conn))
    report=asyncio.run(inspect_historical_stage2(
        normal_source,observed_at=NOW,
    ))
    assert report.status=="V40_HISTORICAL_RESEARCH_SCOPE_OR_LIMIT_INVALID_HALT"
    assert conn.queries==[]


@pytest.mark.parametrize("args",[
    {"lookback_days":8},
    {"lookback_days":0},
    {"max_records":9},
    {"max_records":0},
    {"max_records":True},
])
def test_strict_research_bounds_halt_before_sql(args):
    conn=HistoricalConn()
    report=run(conn,**args)
    assert report.status=="V40_HISTORICAL_RESEARCH_SCOPE_OR_LIMIT_INVALID_HALT"
    assert conn.queries==[]


def test_db_failure_redacted_and_no_bare_raw_sql_errors():
    conn=HistoricalConn()
    conn.fail_query=True
    report=run(conn)
    assert report.status=="V40_HISTORICAL_READONLY_DB_UNAVAILABLE_HALT"
    assert "secret" not in str(report).lower()
    assert report.mexc_posts==report.database_writes==0


def test_invalid_history_response_blocks_entire_sample():
    conn=HistoricalConn()
    conn.history="not-a-row-sequence"
    report=run(conn)
    assert report.status=="V40_HISTORICAL_CANDLE_READBACK_INVALID_HALT"


def test_valid_historical_signal_ages_do_not_pollute_live_age_gate():
    from app.t100_live_v40_stage2_source import inspect_stage2_candidate,V40SignalIntakeHalted
    signal,bars=fixtures()
    with pytest.raises(V40SignalIntakeHalted,match="CLOCK_OR_AGE"):
        inspect_stage2_candidate(signal,bars,observed_at=NOW)
    report=run(HistoricalConn(rows=[signal],bars=bars))
    assert report.reconstructed_records==1
    assert not report.fresh_signal_discovery_verified

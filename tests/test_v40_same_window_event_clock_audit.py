"""Cross-model funding event clock audit: no DB or exchange connections."""
from copy import deepcopy

import pytest

from scripts.v40_same_window_event_clock_audit import audit_timing
from scripts.v40_same_window_pg_replay import (
    BAR, ReplayDataError, replay,
)
from scripts.v40_same_window_chrono_capacity_replay import comparison
from tests.test_v40_same_window_pg_replay import sample


def inputs(*, funding=False):
    src=sample(stop_gap=True)
    if funding:
        signal=int(src["signals"][0]["t"])
        src["funding"]=[{
            "symbol":"SIREN_USDT",
            "t":signal+BAR,
            "rate":.001,
        }]
    return src, replay(src,initial_equity=2000.), comparison(src,capital=2000.)


def test_late_prior_close_observes_one_extra_funding_settlement():
    source,prior,enhanced=inputs(funding=True)
    report=audit_timing(source,prior,enhanced)
    assert len(report["scenarios"])==6
    for result in report["scenarios"]:
        assert result["status"]=="MODELED_SETTLEMENT_WINDOWS_EXPLAIN_EVENT_COUNT_DIFFERENCE"
        assert result["trades_compared"]==1
        assert result["reported_old_funding_events"]==1
        assert result["reported_new_funding_events"]==0
        assert result["events_between_exit_boundaries"]==1
        assert result["exit_timestamp_delta_seconds_counts"]=={"-1800":1}
        changed=result["changed_trade_events"]
        assert len(changed)==1
        assert changed[0]["chronological_minus_prior_exit_seconds"]==-BAR
        assert changed[0]["source_events_between_exit_boundaries"]==1
    assert report["original_v40_simulator_clock_verified"] is False


def test_different_close_timestamps_without_funding_are_still_reported():
    source,prior,enhanced=inputs()
    report=audit_timing(source,prior,enhanced)
    for result in report["scenarios"]:
        assert result["reported_old_funding_events"]==0
        assert result["reported_new_funding_events"]==0
        assert result["exit_timestamp_delta_seconds_counts"]=={"-1800":1}
        assert result["events_between_exit_boundaries"]==0


def test_duplicate_funding_records_are_rejected():
    source,prior,enhanced=inputs(funding=True)
    source["funding"].append(deepcopy(source["funding"][0]))
    with pytest.raises(ReplayDataError,match="DUPLICATE_HISTORICAL_FUNDING_ROW"):
        audit_timing(source,prior,enhanced)


def test_spoofed_funding_event_count_cannot_be_explained():
    source,prior,enhanced=inputs(funding=True)
    prior["scenarios"][0]["counts"]["funding_events"]+=2
    out=audit_timing(source,prior,enhanced)
    result=next(x for x in out["scenarios"]
                if x["cost_bps_per_side"]==100 and x["lot_mode"]=="REFERENCE")
    assert result["status"]=="FUNDING_EVENT_TIMING_DISCREPANCY_UNEXPLAINED"


def test_different_trade_ids_are_not_silently_matched():
    source,prior,enhanced=inputs()
    enhanced["scenarios"][0]["enhanced_chronological"]["trades"][0]["signal_id"]=999
    out=audit_timing(source,prior,enhanced)
    result=next(x for x in out["scenarios"]
                if x["cost_bps_per_side"]==100 and x["lot_mode"]=="REFERENCE")
    assert result["status"]=="TRADE_SELECTION_OR_INVENTORY_DIVERGED"
    assert result["earlier_ids_only"]==[42]
    assert result["later_ids_only"]==[999]

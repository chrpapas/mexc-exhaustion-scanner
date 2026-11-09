"""Fail-closed, GET/SELECT-only V40 stop amendment recovery tests."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal as D

import pytest

from app.t100_live_four_slot_snapshot_coordinator import LedgerSnapshot
from app.t100_live_stop_journal import StopAmendment
from app.t100_live_v40_native_stop_recovery import (
    V40NativeStopRecoveryAuditor,
)
from tests.test_t100_live_stop_update_pipeline import (
    cp, stop, finished_old,
    gates,
)
from tests.test_t100_live_trailing_preflight import report


def amendment():
    return StopAmendment.build(
        symbol="SIREN_USDT",position_id="10",stop_order_id="99",
        previous_stop=D("175"),proposed_stop=D("90"),
    )


def position():
    return {
        "positionId":"10","symbol":"SIREN_USDT",
        "positionType":2,"state":1,"openType":2,
        "leverage":1,"holdVol":"20",
    }


class Journal:
    def __init__(self,state="UNKNOWN"):
        self.state_value=state
        self.calls=[]
    async def state(self,external_id):
        self.calls.append(("state",external_id))
        return self.state_value
    async def mark_verified(self,*_):
        raise AssertionError("read-only recovery must never change journal")
    async def transition(self,*_):
        raise AssertionError("read-only recovery must never change journal")
    async def prepare(self,*_):
        raise AssertionError("read-only recovery must never change journal")


class Ledger:
    def __init__(self,a,*,state="UNKNOWN"):
        self.calls=0
        self.previous=cp()
        self.rows=[asdict(self.previous)]
        self.amendments=[{
            "external_id":a.external_id,
            "symbol":a.symbol,"position_id":a.position_id,
            "stop_order_id":a.stop_order_id,
            "previous_stop":a.previous_stop,
            "proposed_stop":a.proposed_stop,
            "verified_stop_order_id":None,
            "state":state,
        }]
        self.mutate_between_reads=False
        self.readonly=True
    async def read_snapshot(self):
        self.calls+=1
        x=LedgerSnapshot(
            checkpoints=deepcopy(self.rows),
            revisions=[],
            intents=[],
            stop_amendments=deepcopy(self.amendments),
            readonly_repeatable_read=self.readonly,
        )
        if self.mutate_between_reads and self.calls==2:
            return LedgerSnapshot(
                checkpoints=deepcopy(self.rows),
                revisions=[],
                intents=[],
                stop_amendments=[dict(self.amendments[0],state="VERIFIED")],
                readonly_repeatable_read=True,
            )
        return x


class Venue:
    def __init__(self,*,price=D("90"),stop_id="99"):
        self.positions=[position()]
        self.stops=[stop(stopLossPrice=price,id=stop_id)]
        self.calls=[]
        self.drift=False
    async def open_positions(self):
        self.calls.append("positions")
        return deepcopy(self.positions)
    async def open_stop_orders(self):
        self.calls.append("stops")
        result=deepcopy(self.stops)
        if self.drift and self.calls.count("stops")==2:
            result[0]["stopLossPrice"]=D("80")
        return result


class Completeness:
    def __init__(self):
        self.data=report(1)
        self.calls=0
    async def inspect(self):
        self.calls+=1
        return deepcopy(self.data)


class History:
    def __init__(self,records=None):
        self.records=finished_old() if records is None else records
        self.calls=[]
    async def finished_for_stop(self,*,symbol,stop_order_id):
        self.calls.append((symbol,stop_order_id))
        return [deepcopy(self.records)]


def make(*,state="UNKNOWN",price=D("90"),stop_id="99",history=None):
    a=amendment()
    ledger=Ledger(a,state=state)
    venue=Venue(price=price,stop_id=stop_id)
    completeness=Completeness()
    journal=Journal(state=state)
    auditor=V40NativeStopRecoveryAuditor(
        ledger=ledger,venue=venue,
        completeness=completeness,journal=journal,
        finished_stops=history,
    )
    return a,auditor,ledger,venue,completeness,journal


def observe(a,auditor):
    return asyncio.run(auditor.observe(a))


def test_unknown_post_can_observe_native_market_stop_but_never_clear_journal():
    a,reader,ledger,venue,complete,journal=make()
    x=observe(a,reader)
    assert x.status=="V40_AMENDMENT_OUTCOME_OBSERVED_NATIVE_PROTECTION_NONAUTHORIZING"
    assert x.outcome_price_tightened is True
    assert x.all_owned_native_stops_observed is True
    assert x.ledger_bookends_stable and x.venue_bookends_stable
    assert x.account_wide_completeness_observed
    assert x.active_native_market_stops==1
    assert x.owned_positions==1
    assert x.target_stop_order_id=="99"
    assert not x.post_outcome_persistence_allowed
    assert not x.exchange_stop_atomicity_verified
    assert not x.live_stop_mutation_allowed
    assert not x.entry_allowed
    assert not x.live_authorization
    assert ledger.calls==2
    assert venue.calls==["positions","stops","positions","stops"]
    assert complete.calls==1
    assert journal.calls==[("state",a.external_id)]
    assert journal.state_value=="UNKNOWN"


def test_prepared_journal_cannot_infer_success_from_matching_price():
    a,reader,ledger,venue,complete,journal=make(state="PREPARED")
    x=observe(a,reader)
    assert x.status=="V40_PREPARED_WITHOUT_OWNED_MUTATION_RECONCILE_ONLY"
    assert not x.outcome_price_tightened
    assert venue.calls==[]
    assert ledger.calls==0
    assert journal.state_value=="PREPARED"


def test_native_old_stop_still_protects_but_ambiguous_post_blocks_new_entry():
    a,reader,*_=make(price=D("175"))
    x=observe(a,reader)
    assert x.status=="V40_NATIVE_SL_ACTIVE_AMENDMENT_UNRESOLVED_FREEZE"
    assert x.all_owned_native_stops_observed
    assert not x.outcome_price_tightened
    assert not x.entry_allowed


@pytest.mark.parametrize("fault",[
    "no_stop","wrong_market_type","wrong_error_code",
    "undercovered","wrong_margin_mode","incomplete_counts",
    "extra_position","extra_stop","changed_stop",
    "ledger_drift","ledger_role","stale_checkpoint",
    "different_amendment_price","different_amendment_state",
])
def test_any_protection_or_durable_drift_freezes(fault):
    a,reader,ledger,venue,complete,journal=make()
    if fault=="no_stop":
        venue.stops=[]
    elif fault=="wrong_market_type":
        venue.stops[0]["stopLossType"]=1
    elif fault=="wrong_error_code":
        venue.stops[0]["errorCode"]=12
    elif fault=="undercovered":
        venue.stops[0]["vol"]=D("10")
    elif fault=="wrong_margin_mode":
        venue.positions[0]["openType"]=1
    elif fault=="incomplete_counts":
        complete.data["account_wide_completeness_verified"]=False
    elif fault=="extra_position":
        venue.positions.append(dict(position(),positionId="11"))
    elif fault=="extra_stop":
        venue.stops.append(dict(stop(),id="100",positionId="11"))
    elif fault=="changed_stop":
        venue.drift=True
    elif fault=="ledger_drift":
        ledger.mutate_between_reads=True
    elif fault=="ledger_role":
        ledger.readonly=False
    elif fault=="stale_checkpoint":
        ledger.rows[0]["stop_price"]=D("180")
    elif fault=="different_amendment_price":
        ledger.amendments[0]["proposed_stop"]=D("75")
    elif fault=="different_amendment_state":
        ledger.amendments[0]["state"]="PREPARED"

    result=observe(a,reader)
    assert result.status.endswith("_FREEZE")
    assert not result.outcome_price_tightened
    assert not result.post_outcome_persistence_allowed
    assert not result.live_authorization
    assert journal.state_value=="UNKNOWN"


def test_replacement_requires_positive_unique_cancel_provenance():
    a,reader,ledger,venue,complete,journal=make(stop_id="100")
    x=observe(a,reader)
    assert x.status=="V40_STOP_REPLACEMENT_HISTORY_REQUIRED_FREEZE"
    a,reader,ledger,venue,complete,journal=make(
        stop_id="100",history=History(),
    )
    result=observe(a,reader)
    assert result.status=="V40_AMENDMENT_OUTCOME_OBSERVED_NATIVE_PROTECTION_NONAUTHORIZING"
    assert result.target_stop_order_id=="100"
    assert not result.post_outcome_persistence_allowed
    assert not result.live_stop_mutation_allowed


def test_replacement_with_triggered_or_wrong_old_history_not_confirmed():
    a,reader,*_=make(
        stop_id="100",history=History(dict(finished_old(),triggerSide=1)),
    )
    result=observe(a,reader)
    assert result.status=="V40_NATIVE_SL_ACTIVE_AMENDMENT_UNRESOLVED_FREEZE"


def test_two_owned_positions_need_both_protected_market_sl():
    a,reader,ledger,venue,complete,journal=make()
    second=dict(asdict(cp()),symbol="SECOND_USDT",
                external_oid="entry-2",exchange_order_id="order-2",
                position_id="11",stop_order_id="100")
    ledger.rows.append(second)
    venue.positions.append(dict(position(),positionId="11",symbol="SECOND_USDT"))
    venue.stops.append(dict(
        stop(),id="100",positionId="11",symbol="SECOND_USDT",
        stopLossPrice=D("175"),
    ))
    complete.data=report(2)
    result=observe(a,reader)
    assert result.status=="V40_AMENDMENT_OUTCOME_OBSERVED_NATIVE_PROTECTION_NONAUTHORIZING"
    assert result.owned_positions==2
    venue.stops[1]["vol"]="19"
    result=observe(a,reader)
    assert result.status=="V40_STOP_RECOVERY_NATIVE_MARKET_FULL_VOLUME_UNVERIFIED_FREEZE"


def test_readonly_restart_recovery_on_verified_journal_is_non_authorizing():
    a,reader,ledger,venue,complete,journal=make(state="VERIFIED")
    ledger.amendments[0]["verified_stop_order_id"]="99"
    x=observe(a,reader)
    assert x.status=="V40_AMENDMENT_OUTCOME_OBSERVED_NATIVE_PROTECTION_NONAUTHORIZING"
    assert x.journal_state=="VERIFIED"
    assert not x.post_outcome_persistence_allowed


def test_unavailable_signed_get_is_a_freeze_with_no_mutations():
    a,reader,ledger,venue,complete,journal=make()

    async def failed():
        raise TimeoutError("SECRET_MEXC_API_KEY_SHOULD_NOT_APPEAR")

    venue.open_stops=failed
    # Method used by auditor; ensure all transport errors stay redacted.
    venue.open_stop_orders=failed
    x=observe(a,reader)
    assert x.status=="V40_STOP_RECOVERY_READ_OR_PROVENANCE_UNVERIFIED_FREEZE"
    assert not x.entry_allowed


def test_competing_unknown_stop_mutation_freezes_even_with_good_target_stop():
    a,reader,ledger,venue,complete,journal=make()
    ledger.amendments.append({
        "external_id":"other-unknown","symbol":"OTHER_USDT",
        "position_id":"33","stop_order_id":"77",
        "previous_stop":D("180"),"proposed_stop":D("170"),
        "verified_stop_order_id":None,"state":"UNKNOWN",
    })
    result=observe(a,reader)
    assert result.status=="V40_STOP_RECOVERY_COMPETING_UNKNOWN_MUTATION_FREEZE"
    assert result.outcome_price_tightened is False
    assert journal.state_value=="UNKNOWN"


def test_verified_journal_result_stop_id_must_match_current_native_order():
    a,reader,ledger,venue,complete,journal=make(state="VERIFIED")
    ledger.amendments[0]["verified_stop_order_id"]="999"
    result=observe(a,reader)
    assert result.status=="V40_STOP_RECOVERY_VERIFIED_ID_DRIFT_FREEZE"
    assert not result.post_outcome_persistence_allowed
    assert journal.state_value=="VERIFIED"


def test_verified_replacement_record_does_not_bypass_finished_history():
    a,reader,ledger,venue,complete,journal=make(
        state="VERIFIED",stop_id="100",
    )
    ledger.amendments[0]["verified_stop_order_id"]="100"
    result=observe(a,reader)
    assert result.status=="V40_STOP_REPLACEMENT_HISTORY_REQUIRED_FREEZE"
    assert not result.live_authorization


def test_bad_first_stop_error_code_cannot_be_hidden_by_good_second_get():
    a,reader,ledger,venue,complete,journal=make()
    original=venue.open_stop_orders
    calls=[0]

    async def first_bad_second_good():
        calls[0]+=1
        rows=await original()
        if calls[0]==1:
            rows[0]["errorCode"]=2030
        return rows

    venue.open_stop_orders=first_bad_second_good
    result=observe(a,reader)
    # Native stop errorCode is intentionally absent from the shared
    # critical inventory hash, so each bookend requires a separate
    # full-volume MARKET SL audit.
    assert result.status=="V40_STOP_RECOVERY_NATIVE_MARKET_FULL_VOLUME_UNVERIFIED_FREEZE"
    assert result.outcome_price_tightened is False
    assert journal.state_value=="UNKNOWN"


def test_bad_first_stop_market_semantics_cannot_be_hidden_by_second_get():
    a,reader,ledger,venue,complete,journal=make()
    original=venue.open_stop_orders
    calls=[0]

    async def first_bad_second_good():
        calls[0]+=1
        rows=await original()
        if calls[0]==1:
            rows[0]["stopLossType"]=1
        return rows

    venue.open_stop_orders=first_bad_second_good
    result=observe(a,reader)
    assert result.status=="V40_STOP_RECOVERY_NATIVE_MARKET_FULL_VOLUME_UNVERIFIED_FREEZE"
    assert not result.outcome_price_tightened

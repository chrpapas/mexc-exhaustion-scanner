"""Offline V40 one-shot integration tests with mock SQL and exchange.

Mocking the permit/parity gate in these tests is NOT venue certification.
Shipped production status still prohibits actual POSTs.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

import app.t100_live_v40_entry_coordinator as m
from app.t100_live_authorization import LiveAuthorizationHalted
from app.t100_live_execution import EntryIntent,LiveCaps,deterministic_t100_entry_oid
from app.t100_live_four_slot_reservations import SlotReservation
from app.t100_live_four_slot_attempt_journal import DurableAttemptStage
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
from app.t100_live_leader_fencing import LeaderLease
from app.t100_live_v40_dispatch_executor import V40CurrentRiskEvidence
from app.t100_live_v40_stage2_source import V40SourceBatch,inspect_stage2_candidate
from app.t100_live_v40_candidate_market_witness import V40CandidateQuote,V40ExactDepth
from tests.test_t100_live_v40_stage2_source import fixtures,T


def candidate():
    signal,hist=fixtures()
    return inspect_stage2_candidate(
        signal,hist,observed_at=T+timedelta(seconds=25),
    )


def inventory():
    return {
        "account_wide_completeness_verified":True,
        "critical_inventory_equal_across_witness_window":True,
        "after_positions":0,"after_current_tpsl":0,
        "open_order_counts":{
            "sumCount":0,"stopOrderCount":0,"limitOrderCount":0,
            "planOrderCount":0,"trackOrderCount":0,
        },
    }


def snapshot():
    return FourSlotSnapshotResult(
        status="FLAT_OBSERVED_NONAUTHORIZING",
        occupied_slots=0,inventory=inventory(),
        ledger_hash="a"*64,critical_inventory_hash="b"*64,
    )


def risk(*,equity="10000",collateral="10000",book_age=100):
    return V40CurrentRiskEvidence(
        current_account_equity_usdt=D(equity),
        available_margin_usdt=D(collateral),
        portfolio_notional_usdt=D("0"),
        realized_today_loss_usdt=D("0"),
        current_owned_positions=0,
        signal_age_seconds=25.0,book_age_ms=book_age,
        observed_sell_vwap=D("100"),
        inventory_bookended_complete=True,
        no_unowned_positions_or_orders=True,
        cross_collateral_verified=True,
        private_risk_tier_verified=True,
        contract_trading_access_verified=True,
        hedge_mode_verified=True,
        no_unresolved_mutations=True,
    )


def terms(**kw):
    args={
        "contract_size":D("1"),"volume_step":D("1"),
        "min_volume":D("1"),"max_volume":D("10000"),
        "price_tick":D(".01"),
        "private_order_permission_verified":True,
        "market_state_and_country_verified":True,
    }
    args.update(kw)
    return m.V40ContractTerms(**args)


def caps():
    return LiveCaps(
        max_order_usdt=D("10000"),
        max_portfolio_usdt=D("20000"),
        max_daily_loss_usdt=D("10000"),
    )


def test_prepare_candidate_matches_exact_frozen_stage2_reference_and_contract():
    picked=m.prepare_candidate(
        signal=candidate(),witness=snapshot(),
        risk=risk(),terms=terms(),cost_bps=100.,
    )
    assert type(picked) is m.V40PreparedEntry
    assert picked.plan.action=="ADMIT"
    assert picked.plan.modeled_notional_usdt==5000
    assert picked.intent.notional_usdt==D("5000")
    assert picked.intent.volume_contracts==D("50")
    assert picked.intent.stop_price==D("175")
    assert picked.intent.external_oid==deterministic_t100_entry_oid(42,"EXAMPLE_USDT")
    assert picked.entry_allowed is False
    assert picked.live_authorization is False


def test_preparation_fails_without_current_contract_permission():
    with pytest.raises(m.V40CoordinatorHalted,match="CONTRACT_TERMS"):
        m.prepare_candidate(
            signal=candidate(),witness=snapshot(),
            risk=risk(),
            terms=terms(private_order_permission_verified=False),
            cost_bps=100.,
        )


def test_preparation_requires_full_account_witness_not_just_equity():
    with pytest.raises(m.V40CoordinatorHalted,match="ACCOUNT_WITNESS"):
        m.prepare_candidate(
            signal=candidate(),witness=snapshot(),
            risk=replace(risk(),cross_collateral_verified=False),
            terms=terms(),cost_bps=100.,
        )


def test_rounding_down_is_compatible_with_durable_journal_exact_notional():
    item=m.prepare_candidate(
        signal=candidate(),witness=snapshot(),
        risk=risk(),terms=terms(contract_size=D("3")),cost_bps=100.,
    )
    assert item.intent.notional_usdt==D("4800")
    assert item.intent.volume_contracts==D("16")
    assert item.intent.notional_usdt==(
        item.intent.volume_contracts*item.terms.contract_size*
        item.intent.reference_price
    )


class DummySource:
    def __init__(self, events, log):
        self.events=events
        self.log=log
    async def latest_candidates(self,*,observed_at):
        self.log.append("source")
        return V40SourceBatch(
            "FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING",
            observed_at=observed_at,
            candidates=tuple(self.events),
        )


class DummySnapshots:
    def __init__(self,log,after_status="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"):
        self.log=log
        self.count=0
        self.after_status=after_status
    async def inspect(self):
        self.count+=1
        self.log.append("snapshot_"+str(self.count))
        if self.count==1:
            return snapshot()
        return FourSlotSnapshotResult(
            status=self.after_status,
            recovery=SimpleNamespace(recovered=(SimpleNamespace(symbol="EXAMPLE_USDT"),))
            if self.after_status=="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"
            else None,
        )


class DummyRiskProvider:
    def __init__(self,log,second_risk=None,second_terms=None):
        self.log=log
        self.count=0
        self.second_risk=second_risk
        self.second_terms=second_terms
    async def inspect(self,signal):
        assert signal.signal_id==42
        self.count+=1
        self.log.append("risk_"+str(self.count))
        return (
            self.second_risk if self.count==2 and self.second_risk is not None
            else risk(),
            self.second_terms if self.count==2 and self.second_terms is not None
            else terms(),
        )


class DummyDepth:
    def __init__(self,log,*,fault=None):
        self.log=log
        self.fault=fault

    async def quote_exact_intent(self,signal,intent,*,max_slippage_bps):
        self.log.append("depth")
        if self.fault=="timeout":
            raise TimeoutError("SENSITIVE_MEXC_CLIENT_RESPONSE")
        assert max_slippage_bps==D("100")
        assert intent.symbol==signal.symbol
        assert intent.volume_contracts==D("50")
        real_terms=terms()
        public_terms=replace(real_terms,private_order_permission_verified=False)
        quote=V40CandidateQuote(
            status="V40_GET_ONLY_FLAT_ACCOUNT_AND_MARKET_QUOTED_NONAUTHORIZING",
            symbol="OTHER_USDT" if self.fault=="wrong_symbol" else signal.symbol,
            observed_at_ms=998000 if self.fault=="stale" else 999900,
            book_age_ms=100,
            account_equity_usdt=D("9999") if self.fault=="equity" else D("10000"),
            account_available_usdt=D("4500") if self.fault=="margin" else D("10000"),
            candidate_contract=(
                replace(public_terms,contract_size=D("2"))
                if self.fault=="contract" else public_terms
            ),
            current_private_short_max_vol=D("1000"),
            top_bid=D("100"),top_ask=D("101"),
            bookended_flat_account=True,
            hedge_mode_observed=True,private_risk_row_observed=True,
        )
        depth=V40ExactDepth(
            status="V40_EXECUTABLE_LOT_DISPLAYED_DEPTH_NONAUTHORIZING",
            symbol=signal.symbol,
            signal_id=signal.signal_id,
            contracts=D("49") if self.fault=="quantity" else intent.volume_contracts,
            sell_vwap=D("99") if self.fault=="slippage" else D("100"),
            adverse_slippage_bps=D("200") if self.fault=="slippage" else D(0),
            age_ms=100,order_fits_depth=True,
        )
        return quote,depth


class DummyLeaders:
    def __init__(self,log):
        self.log=log
    async def acquire(self,holder_id):
        self.log.append("lease_acquire")
        return LeaderLease(
            lease_name="t100-live-executor",holder_id=holder_id,
            fencing_token=3,ttl_seconds=45,
        )
    async def require_current(self,lease):
        self.log.append("lease_require")
    async def renew(self,lease):
        self.log.append("lease_renew")
    async def release(self,lease):
        self.log.append("lease_release")


class DummyReservations:
    def __init__(self,log,corrupt_oid=False):
        self.log=log
        self.corrupt_oid=corrupt_oid
    async def reserve(self, **kwargs):
        self.log.append("reserve")
        sig=kwargs["signal_id"]
        sym=kwargs["symbol"]
        return SlotReservation(
            signal_id=sig,symbol=sym,
            external_oid=(
                "wrong" if self.corrupt_oid
                else deterministic_t100_entry_oid(sig,sym)
            ),
            modeled_notional_usdt=D("5000"),
            occupied_after_reservation=1,
        )


class DummyJournal:
    def __init__(self,log):
        self.log=log
    async def stage(self, **kwargs):
        self.log.append("stage")
        intent=kwargs["intent"]
        assert intent.notional_usdt==(
            intent.volume_contracts*kwargs["contract_size"]*
            intent.reference_price
        )
        return DurableAttemptStage(
            signal_id=intent.signal_id,external_oid=intent.external_oid,
        )


def controller(log, *, events=None, second_risk=None, second_terms=None,
               corrupt_oid=False, after_status=None, depth_fault=None,
               clock_ms=None):
    return m.V40OneShotCoordinator(
        source=DummySource([candidate()] if events is None else events,log),
        snapshots=DummySnapshots(log,after_status or "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"),
        witness=DummyRiskProvider(log,second_risk,second_terms),
        depth=DummyDepth(log,fault=depth_fault),
        clock_ms=clock_ms if clock_ms is not None else lambda: 1_000_000,
        leaders=DummyLeaders(log),
        reservations=DummyReservations(log,corrupt_oid=corrupt_oid),
        journal=DummyJournal(log),
        claims=SimpleNamespace(),
        claim_reader=SimpleNamespace(),
        transport=SimpleNamespace(),
        get_venue=SimpleNamespace(),
        completeness=SimpleNamespace(),
        commit_writer=SimpleNamespace(),
    )


def test_production_uncertified_permit_blocks_before_any_source_or_db_action():
    log=[]
    with pytest.raises(LiveAuthorizationHalted):
        asyncio.run(controller(log).run_one(
            permit=None,caps=caps(),parity_report={},holder_id="test-holder",
        ))
    assert log==[]


def patch_release_gates_and_mutations(monkeypatch,log):
    # Tests ONLY: explicit monkeypatching is not a certification mechanism.
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda _:None)
    monkeypatch.setattr(m,"assess_strategy_parity_report",
                        lambda _:{"strategy_parity_verified":True})

    async def dispatch(**kwargs):
        log.append("dispatch")
        assert kwargs["stage"].external_oid==kwargs["intent"].external_oid
        return SimpleNamespace(must_reconcile_external_oid=True)

    async def reconcile(**kwargs):
        log.append("reconcile")
        return SimpleNamespace(
            status="V40_PROTECTED_FILL_AND_CHECKPOINT_PERSISTED_RESTART_REQUIRED"
        )

    monkeypatch.setattr(m,"dispatch_one_staged_v40",dispatch)
    monkeypatch.setattr(m,"reconcile_and_commit_protected_v40",reconcile)


def test_injected_one_shot_ordered_durable_path_with_fresh_recovery(monkeypatch):
    log=[]
    patch_release_gates_and_mutations(monkeypatch,log)
    outcome=asyncio.run(controller(log).run_one(
        permit=None,caps=caps(),parity_report={},holder_id="mock-holder",
    ))
    assert outcome.status=="V40_ONE_PROTECTED_ENTRY_RECOVERED_NOT_AUTONOMOUS"
    assert outcome.post_attempted is True
    assert outcome.independently_recovered_after_commit is True
    assert outcome.entry_allowed is False
    assert outcome.live_authorization is False
    assert log==[
        "source","lease_acquire","snapshot_1","risk_1",
        "lease_require","reserve","stage","risk_2",
        "depth","lease_renew","dispatch","reconcile","snapshot_2",
    ]
    assert "lease_release" not in log


def test_reservation_origin_mismatch_stops_before_stage_and_post(monkeypatch):
    log=[]
    patch_release_gates_and_mutations(monkeypatch,log)
    outcome=asyncio.run(controller(log,corrupt_oid=True).run_one(
        permit=None,caps=caps(),parity_report={},holder_id="mock-holder",
    ))
    assert outcome.status=="V40_RESERVED_ORIGIN_MISMATCH_MANUAL_RECOVERY_ONLY"
    assert "reserve" in log
    assert "stage" not in log
    assert "dispatch" not in log
    assert "lease_release" not in log


def test_refresh_contract_terms_changed_after_stage_never_posts(monkeypatch):
    log=[]
    patch_release_gates_and_mutations(monkeypatch,log)
    outcome=asyncio.run(controller(
        log,second_terms=terms(price_tick=D(".001")),
    ).run_one(
        permit=None,caps=caps(),parity_report={},holder_id="mock-holder",
    ))
    assert outcome.status=="V40_CONTRACT_TERMS_CHANGED_AFTER_STAGE_NO_POST"
    assert "stage" in log and "dispatch" not in log


def test_refresh_reduced_equity_blocks_old_notional_after_staging(monkeypatch):
    log=[]
    patch_release_gates_and_mutations(monkeypatch,log)
    outcome=asyncio.run(controller(
        log,second_risk=risk(equity="100"),
    ).run_one(
        permit=None,caps=caps(),parity_report={},holder_id="mock-holder",
    ))
    assert outcome.status=="V40_REFRESHED_V40_CAPACITY_OR_COLLATERAL_REJECTS_POST"
    assert "stage" in log and "dispatch" not in log


def test_after_commit_uncertified_account_recovery_halts_next_entry(monkeypatch):
    log=[]
    patch_release_gates_and_mutations(monkeypatch,log)
    outcome=asyncio.run(controller(
        log,after_status="UNOWNED_OR_MISSING_CHECKPOINTS",
    ).run_one(
        permit=None,caps=caps(),parity_report={},holder_id="mock-holder",
    ))
    assert outcome.status=="V40_CHECKPOINT_COMMITTED_RESTART_UNVERIFIED_FREEZE"
    assert outcome.post_attempted
    assert not outcome.independently_recovered_after_commit


def test_no_stage2_signal_exits_before_leader_acquisition(monkeypatch):
    log=[]
    patch_release_gates_and_mutations(monkeypatch,log)
    outcome=asyncio.run(controller(log,events=[]).run_one(
        permit=None,caps=caps(),parity_report={},holder_id="mock-holder",
    ))
    assert outcome.status=="V40_NO_CURRENT_STAGE2_SIGNALS"
    assert log==["source"]


@pytest.mark.parametrize("fault",[
    "stale","wrong_symbol","equity","margin","contract",
    "quantity","slippage","timeout",
])
def test_exact_quantity_market_witness_failure_never_claims_or_posts(
    monkeypatch,fault,
):
    log=[]
    patch_release_gates_and_mutations(monkeypatch,log)
    outcome=asyncio.run(controller(log,depth_fault=fault).run_one(
        permit=None,caps=caps(),parity_report={},holder_id="mock-holder",
    ))
    assert outcome.status=="V40_EXACT_EXECUTABLE_DEPTH_OR_ACCOUNT_UNVERIFIED_FREEZE"
    assert outcome.reserved and outcome.staged
    assert outcome.post_attempted is False
    assert "depth" in log
    assert "dispatch" not in log
    assert "lease_release" not in log


def test_exact_vwap_is_passed_to_dispatch_not_preliminary_top_bid(monkeypatch):
    log=[]
    patch_release_gates_and_mutations(monkeypatch,log)

    async def dispatch(**kwargs):
        log.append("dispatch")
        assert kwargs["risk"].observed_sell_vwap==D("100")
        assert kwargs["risk"].cross_collateral_verified is True
        assert kwargs["risk"].private_risk_tier_verified is True
        assert kwargs["risk"].no_unresolved_mutations is True
        assert kwargs["risk"].book_age_ms==100
        return SimpleNamespace(must_reconcile_external_oid=True)

    monkeypatch.setattr(m,"dispatch_one_staged_v40",dispatch)
    result=asyncio.run(controller(log).run_one(
        permit=None,caps=caps(),parity_report={},holder_id="mock-holder",
    ))
    assert result.independently_recovered_after_commit
    assert log.count("depth")==1


def test_exact_depth_mandatory_at_constructor():
    log=[]
    with pytest.raises((TypeError,ValueError)):
        m.V40OneShotCoordinator(
            source=DummySource([],log),snapshots=DummySnapshots(log),
            witness=DummyRiskProvider(log),depth=None,
            leaders=DummyLeaders(log),
            reservations=DummyReservations(log),
            journal=DummyJournal(log),
            claims=None,claim_reader=None,
            transport=None,get_venue=None,
            completeness=None,commit_writer=None,
        )


def test_depth_becomes_stale_during_lease_renewal_and_never_posts(monkeypatch):
    log=[]
    patch_release_gates_and_mutations(monkeypatch,log)
    readings=iter((1_000_000,1_002_000))
    result=asyncio.run(controller(
        log,clock_ms=lambda:next(readings),
    ).run_one(
        permit=None,caps=caps(),parity_report={},holder_id="mock-holder",
    ))
    assert result.status=="V40_EXACT_EXECUTABLE_DEPTH_STALE_AFTER_LEASE_FREEZE"
    assert result.staged is True
    assert result.post_attempted is False
    assert "lease_renew" in log and "dispatch" not in log


def test_exact_depth_verification_never_upgrades_uncertified_risk_flags():
    import app.t100_live_v40_candidate_market_witness as market
    signal=candidate()
    intent=EntryIntent.build(
        signal_id=signal.signal_id,symbol=signal.symbol,
        reference_price=D("100"),notional_usdt=D("5000"),
        contract_size=D("1"),volume_step=D("1"),
        min_volume=D("1"),max_volume=D("10000"),
        price_tick=D(".01"),
    )
    original=replace(
        risk(),cross_collateral_verified=False,
        private_risk_tier_verified=False,
        contract_trading_access_verified=False,
        no_unresolved_mutations=False,
        observed_sell_vwap=D("0"),
    )
    public_contract=replace(terms(),private_order_permission_verified=False)
    quote=market.V40CandidateQuote(
        status="V40_GET_ONLY_FLAT_ACCOUNT_AND_MARKET_QUOTED_NONAUTHORIZING",
        symbol=signal.symbol,observed_at_ms=999_900,book_age_ms=100,
        account_equity_usdt=D("10000"),account_available_usdt=D("10000"),
        candidate_contract=public_contract,
        current_private_short_max_vol=D("1000"),
        top_bid=D("101"),top_ask=D("102"),
        bookended_flat_account=True,hedge_mode_observed=True,
        private_risk_row_observed=True,
    )
    depth=market.V40ExactDepth(
        status="V40_EXECUTABLE_LOT_DISPLAYED_DEPTH_NONAUTHORIZING",
        symbol=signal.symbol,signal_id=signal.signal_id,
        contracts=intent.volume_contracts,sell_vwap=D("99"),
        adverse_slippage_bps=D("100"),age_ms=100,
        order_fits_depth=True,
    )
    merged=m.verify_fresh_exact_depth(
        signal=signal,intent=intent,risk=original,
        terms=terms(),caps=caps(),quote=quote,
        depth=depth,now_ms=1_000_000,
    )
    assert merged.observed_sell_vwap==D("99")
    assert not merged.cross_collateral_verified
    assert not merged.private_risk_tier_verified
    assert not merged.contract_trading_access_verified
    assert not merged.no_unresolved_mutations
    assert original.observed_sell_vwap==D("0")


def test_exact_monitored_signal_identity_can_progress_only_under_mocked_release(monkeypatch):
    log=[]
    patch_release_gates_and_mutations(monkeypatch,log)
    signal=candidate()
    outcome=asyncio.run(controller(log).run_one(
        permit=None,caps=caps(),parity_report={},holder_id="mock-holder",
        monitored_signal_id=signal.signal_id,
        monitored_symbol=signal.symbol,monitored_tier=signal.tier,
    ))
    assert outcome.status=="V40_ONE_PROTECTED_ENTRY_RECOVERED_NOT_AUTONOMOUS"
    assert log[0:2]==["source","lease_acquire"]
    assert log.count("dispatch")==1


@pytest.mark.parametrize("candidate_variant",[
    "different_id","different_symbol","different_tier",
    "empty","multiple","partial_identity","bad_identity",
])
def test_monitored_signal_drift_blocks_before_lease_reservation_or_post(
    monkeypatch,candidate_variant,
):
    log=[]
    patch_release_gates_and_mutations(monkeypatch,log)
    signal=candidate()
    kwargs={
        "monitored_signal_id":signal.signal_id,
        "monitored_symbol":signal.symbol,
        "monitored_tier":signal.tier,
    }
    events=[signal]
    if candidate_variant=="different_id":
        kwargs["monitored_signal_id"]=signal.signal_id+1
    elif candidate_variant=="different_symbol":
        kwargs["monitored_symbol"]="DIFFERENT_USDT"
    elif candidate_variant=="different_tier":
        kwargs["monitored_tier"]=(
            "HIGH_225" if signal.tier=="LOW_100" else "LOW_100"
        )
    elif candidate_variant=="empty":
        events=[]
    elif candidate_variant=="multiple":
        events=[signal,signal]
    elif candidate_variant=="partial_identity":
        del kwargs["monitored_tier"]
    else:
        kwargs["monitored_signal_id"]=True
    with pytest.raises(m.V40CoordinatorHalted,match="V40_MONITORED_SIGNAL"):
        asyncio.run(controller(log,events=events).run_one(
            permit=None,caps=caps(),parity_report={},holder_id="mock-holder",
            **kwargs,
        ))
    assert log==["source"]


def test_tiny_pilot_lot_uses_capped_size_not_frozen_full_4_slot_target():
    picked=m.prepare_candidate(
        signal=candidate(),witness=snapshot(),
        risk=risk(),terms=terms(contract_size=D(".1")),
        cost_bps=100.,pilot_notional_ceiling=D("12.50"),
    )
    assert picked.plan.modeled_notional_usdt==5000
    assert picked.intent.notional_usdt==D("10")
    assert picked.intent.volume_contracts==D("1")
    assert picked.intent.notional_usdt<=D("12.50")
    assert picked.entry_allowed is False and picked.live_authorization is False


def test_tiny_pilot_never_tops_up_subminimum_grid():
    with pytest.raises(m.V40CoordinatorHalted,match="CONTRACT_LOT"):
        m.prepare_candidate(
            signal=candidate(),witness=snapshot(),risk=risk(),
            terms=terms(contract_size=D("1")),
            cost_bps=100.,pilot_notional_ceiling=D("12.50"),
        )


def test_invalid_pilot_ceiling_blocks_without_order():
    with pytest.raises(m.V40CoordinatorHalted,match="PILOT_CEILING"):
        m.prepare_candidate(
            signal=candidate(),witness=snapshot(),risk=risk(),
            terms=terms(),cost_bps=100.,
            pilot_notional_ceiling=D("-1"),
        )


def test_pre_reservation_small_pilot_notional_refused_before_db_mutation(monkeypatch):
    log=[]
    patch_release_gates_and_mutations(monkeypatch,log)
    small=LiveCaps(
        max_order_usdt=D("12.50"),
        max_portfolio_usdt=D("12.50"),
        max_daily_loss_usdt=D("8"),
    )
    with pytest.raises(m.V40CoordinatorHalted,match="CONTRACT_LOT"):
        asyncio.run(controller(log).run_one(
            permit=None,caps=small,parity_report={},holder_id="mock-holder",
        ))
    assert "reserve" not in log
    assert "stage" not in log
    assert "dispatch" not in log
    assert "lease_release" in log

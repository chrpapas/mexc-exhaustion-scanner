"""No real orders. Strict signal-bound V40 canary one-shot coordinator tests."""
import asyncio
from dataclasses import replace
from datetime import datetime,timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

import app.t100_v40_first_signal_execution as m
from app.t100_live_authorization import V40_CANARY_SCOPE
from app.t100_live_guards import T100ExecutionControls,LIVE_CONFIRMATION
from app.t100_live_execution import EntryIntent
from app.t100_live_v40_entry_coordinator import (
    V40ContractTerms,V40OneShotCoordinator,V40OneShotReport,
)
from app.t100_live_v40_dispatch_executor import V40CurrentRiskEvidence
from app.t100_live_v40_stage2_source import (
    PostgresV40Stage2Source,V40SourceCandidate,V40SourceBatch,
)
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
from app.t100_live_v40_candidate_market_witness import (
    MexcV40CandidateMarketWitness,
)
from app.t100_v40_guardian_post_gate import (
    PostgresV40GuardianPostGate,V40GuardianPostGateEvidence,
)
from app.t100_v40_executor_assembly import (
    V40FirstPilotGetOnly,V40ExecutorAssembly,
)
from app.t100_v40_first_canary_risk_witness import V40FirstCanaryRiskWitness
from app.t100_v40_first_canary_parity_evidence import (
    load_verified_frozen_v40_entry_parity,
)
from app.mexc_trade import MexcTradeClient


class Pool:
    def acquire(self):
        return None


def control():
    return T100ExecutionControls.from_env({
        "T100_EXECUTION_MODE":"live","T100_LIVE_ORDER_API_ENABLED":"true",
        "T100_LIVE_KILL_SWITCH":"false",
        "T100_LIVE_OPERATOR_CONFIRM":LIVE_CONFIRMATION,
        "T100_LIVE_MAX_POSITION_NOTIONAL_USDT":"12.50",
        "T100_LIVE_MAX_TOTAL_NOTIONAL_USDT":"12.50",
        "T100_LIVE_MAX_DAILY_LOSS_USDT":"12.50",
        "T100_LIVE_LEVERAGE":"1",
    })


def signal():
    now=int(datetime.now(timezone.utc).timestamp())
    return V40SourceCandidate(
        signal_id=727,symbol="TEST_USDT",strategy_id="T100_200",
        admission_open_ts=now-120,feature_open_ts=now-1920,
        entry_reference_price=100.,stage1_atr7_pct=5.5,
        stage1_return_24h=0.2,physical_episode_seq=7,
        tier="LOW_100",prior_bars=tuple(),pre24_vol_pct=4.,
        observed_age_seconds=120.,
    )


def tape(*signals):
    return V40SourceBatch(
        status="FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING",
        observed_at=datetime.now(timezone.utc),
        candidates=tuple(signals),
    )


def guardian_good():
    return V40GuardianPostGateEvidence(
        status="V40_GUARDIAN_POST_GATE_CURRENT_FLAT_UNLATCHED_NONAUTHORIZING",
        state_verified=True,outbox_verified=True,
        guardian_fresh=True,critical_latch_absent=True,
    )


def risk():
    return V40CurrentRiskEvidence(
        current_account_equity_usdt=D("56.37671495"),
        available_margin_usdt=D("56.37671495"),
        portfolio_notional_usdt=D(0),
        realized_today_loss_usdt=D(0),
        current_owned_positions=0,signal_age_seconds=120.,
        book_age_ms=100,observed_sell_vwap=D(0),
        inventory_bookended_complete=True,
        no_unowned_positions_or_orders=True,
        cross_collateral_verified=True,
        private_risk_tier_verified=True,
        contract_trading_access_verified=True,
        hedge_mode_verified=True,no_unresolved_mutations=True,
    )


def terms():
    return V40ContractTerms(
        contract_size=D(".001"),volume_step=D("1"),
        min_volume=D("1"),max_volume=D("1000"),price_tick=D(".01"),
        private_order_permission_verified=True,
        market_state_and_country_verified=True,
    )


def approved():
    return m.V40FirstCanaryReleaseReview(
        code_revision_verified=True,
        native_attached_market_stop_request_contract_reviewed=True,
        post_fill_get_and_journal_reconciliation_reviewed=True,
        independent_nonflat_guardian_live_verified=True,
        durable_incident_alert_delivery_verified=True,
        supervised_emergency_exit_runbook_verified=True,
        operator_explicitly_authorized_first_canary=True,
    )


def setup(monkeypatch,*,signals=None,release=None,outcome=None,explode=False):
    """Build inert typed collaborators; no DB, HTTP or exchange mutations."""
    events=[]
    source_pool=Pool()
    runtime=Pool()
    source=PostgresV40Stage2Source(
        source_pool,restricted_source_reader=True,
    )
    async def latest_candidates(*,observed_at):
        events.append("REAL_SOURCE")
        return tape(*((signal(),) if signals is None else signals))
    source.latest_candidates=latest_candidates

    signed_http=SimpleNamespace(base_url="https://api.mexc.com/")
    public=SimpleNamespace(base_url="https://api.mexc.com/",get=lambda *a:None)
    signed=V40FirstPilotGetOnly(
        signed_http,api_key="EXAMPLE_KEY_00001",
        api_secret="EXAMPLE_SECRET_0000000000001",
    )
    market=object.__new__(MexcV40CandidateMarketWitness)
    market.private=SimpleNamespace(_client=signed)
    market.public=public
    async def exact(*args,**kwargs):
        events.append("EXACT_LOT_DEPTH")
        return object(),object()
    market.quote_exact_intent=exact
    witness=object.__new__(V40FirstCanaryRiskWitness)
    witness.market=market
    async def inspect(_):
        events.append("INDEPENDENT_RISK_PNL_KEY_SQL")
        return risk(),terms()
    witness.inspect=inspect

    gate=PostgresV40GuardianPostGate(runtime)
    async def verify():
        events.append("DURABLE_GUARDIAN")
        return guardian_good()
    gate.verify=verify

    venue=object.__new__(MexcTradeClient)
    venue.api_key=signed._key
    venue.api_secret=signed._secret
    venue.client=SimpleNamespace(base_url="https://api.mexc.com")

    coord=object.__new__(V40OneShotCoordinator)
    coord.initial_pilot_only=True
    class Snap:
        async def inspect(self):
            events.append("FULL_ACCOUNT_SNAPSHOT")
            return FourSlotSnapshotResult(
                "FLAT_OBSERVED_NONAUTHORIZING",occupied_slots=0,
            )
    coord.snapshots=Snap()
    async def run_one(**args):
        events.append("ONE_FENCED_DISPATCH")
        if explode:
            raise RuntimeError("AFTER_DB_COMMIT_MEXC_ORDER_UNKNOWN")
        assert args["monitored_signal_id"]==727
        assert args["monitored_symbol"]=="TEST_USDT"
        assert args["monitored_tier"]=="LOW_100"
        assert args["permit"].signal_id==727
        assert args["permit"].symbol=="TEST_USDT"
        return outcome or V40OneShotReport("V40_NO_ADMISSIBLE_STAGE2_CANDIDATE")
    coord.run_one=run_one

    def assembly(**kwargs):
        events.append(
            "LIVE_ASSEMBLY" if kwargs.get("live_transport") else "GETONLY_ASSEMBLY"
        )
        return V40ExecutorAssembly(
            coordinator=coord,
            production_post_adapter_connected=bool(kwargs.get("live_transport")),
        )
    monkeypatch.setattr(m,"assemble_v40_first_pilot",assembly)
    monkeypatch.setattr(m,"prepare_candidate",lambda **kwargs:SimpleNamespace(
        intent=EntryIntent.build(
            signal_id=727,symbol="TEST_USDT",
            reference_price="100",notional_usdt="10",
            contract_size="0.001",volume_step="1",min_volume="1",
            max_volume="1000",price_tick="0.01",
        ),
    ))
    monkeypatch.setattr(m,"verify_fresh_exact_depth",lambda **kwargs:None)
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda p:p)
    # Permit creation itself is still tested separately by its own suite.
    calls=[]
    def issue(c,e,*,signal_id,symbol):
        calls.append((signal_id,symbol,e))
        return SimpleNamespace(
            scope=V40_CANARY_SCOPE,signal_id=signal_id,symbol=symbol,
        )
    monkeypatch.setattr(m,"issue_v40_supervised_canary_permit",issue)

    kwargs=dict(
        source=source,executor_runtime_pool=runtime,
        signed_get=signed,public_get=public,key_and_risk=witness,
        guardian_gate=gate,client=venue,controls=control(),
        parity_report=load_verified_frozen_v40_entry_parity(),
        external_review=approved() if release is None else release,
        holder_id="v40-first-canary-offline-test",
    )
    return kwargs,events,calls


def test_empty_real_source_cannot_touch_account_or_issue_permit(monkeypatch):
    kwargs,events,calls=setup(monkeypatch,signals=())
    result=asyncio.run(m.attempt_one_real_v40_first_signal(**kwargs))
    assert result.status=="V40_FIRST_CANARY_WAIT_FOR_GENUINE_STAGE2"
    assert events==["REAL_SOURCE"]
    assert calls==[]
    assert not result.exchange_post_may_have_occurred


def test_live_release_incomplete_never_constructs_order_transport(monkeypatch):
    kwargs,events,calls=setup(
        monkeypatch,release=m.V40FirstCanaryReleaseReview(),
    )
    result=asyncio.run(m.attempt_one_real_v40_first_signal(**kwargs))
    assert result.status=="V40_FIRST_CANARY_RELEASE_REVIEW_INCOMPLETE_NO_POST"
    assert "LIVE_ASSEMBLY" not in events
    assert "ONE_FENCED_DISPATCH" not in events
    assert calls==[]
    assert result.candidate_prechecked and not result.permit_issued


def test_actual_genuine_identity_goes_to_one_fenced_coordinator(monkeypatch):
    kwargs,events,calls=setup(monkeypatch)
    result=asyncio.run(m.attempt_one_real_v40_first_signal(**kwargs))
    assert result.status=="V40_NO_ADMISSIBLE_STAGE2_CANDIDATE"
    assert len(calls)==1
    assert calls[0][0:2]==(727,"TEST_USDT")
    assert "EXACT_LOT_DEPTH" in events and "DURABLE_GUARDIAN" in events
    assert events.count("ONE_FENCED_DISPATCH")==1
    assert events.index("LIVE_ASSEMBLY")<events.index("ONE_FENCED_DISPATCH")
    assert result.permit_issued and not result.exchange_post_may_have_occurred


def test_unknown_once_claim_boundary_never_allows_another_entry(monkeypatch):
    kwargs,events,calls=setup(monkeypatch,explode=True)
    result=asyncio.run(m.attempt_one_real_v40_first_signal(**kwargs))
    assert result.status=="V40_FIRST_CANARY_UNKNOWN_AFTER_ENTRY_BOUNDARY_RECONCILE_ONLY"
    assert result.reservation_or_dispatch_may_exist is True
    assert result.exchange_post_may_have_occurred is True
    assert result.further_entry_allowed is False
    assert events.count("ONE_FENCED_DISPATCH")==1


def test_protected_commit_recovered_still_does_not_grant_autonomous_mode(monkeypatch):
    kwargs,events,calls=setup(monkeypatch,outcome=V40OneShotReport(
        "V40_ONE_PROTECTED_ENTRY_RECOVERED_NOT_AUTONOMOUS",
        signal_id=727,reserved=True,staged=True,post_attempted=True,
        claim_may_exist=True,independently_recovered_after_commit=True,
    ))
    result=asyncio.run(m.attempt_one_real_v40_first_signal(**kwargs))
    assert result.live_order_outcome_certified is True
    assert result.exchange_post_may_have_occurred is True
    assert result.further_entry_allowed is False
    assert events.count("ONE_FENCED_DISPATCH")==1


def test_not_in_live_mode_fails_before_source_or_network(monkeypatch):
    kwargs,events,calls=setup(monkeypatch)
    bad=T100ExecutionControls.from_env({
        "T100_EXECUTION_MODE":"paper",
        "T100_LIVE_ORDER_API_ENABLED":"false",
        "T100_LIVE_KILL_SWITCH":"true",
    })
    kwargs["controls"]=bad
    result=asyncio.run(m.attempt_one_real_v40_first_signal(**kwargs))
    assert result.status=="V40_FIRST_CANARY_LIVE_CONFIG_OR_ACCOUNT_IDENTITY_HALT"
    assert events==[] and calls==[]


def test_duplicate_real_stage2_batch_halts_before_risk_or_account(monkeypatch):
    s=signal()
    kwargs,events,calls=setup(monkeypatch,signals=(s,replace(s,signal_id=728)))
    result=asyncio.run(m.attempt_one_real_v40_first_signal(**kwargs))
    assert result.status=="V40_FIRST_CANARY_SOURCE_FAILED_NO_POST"
    assert events==["REAL_SOURCE"] and calls==[]

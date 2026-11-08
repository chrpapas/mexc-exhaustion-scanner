"""Offline fault tests for V40 durable claim -> exactly one POST -> reconcile.

No exchange, database, internet, Render service or credentials are touched.
Certification assertions are explicitly mocked for narrow dispatch tests;
the separate unpatched test establishes the default production blocker.
"""
import asyncio
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app import t100_live_v40_dispatch_executor as dispatch
from app.t100_live_execution import EntryIntent, LiveCaps
from app.t100_live_four_slot_planner import FourSlotPlan
from app.t100_live_four_slot_attempt_journal import DurableAttemptStage
from app.t100_live_four_slot_dispatch_claims import FourSlotDispatchClaim
from app.t100_live_leader_fencing import LeaderLease
from app.t100_v40_admission import V40Admission


def setup(monkeypatch, *, certify=True):
    if certify:
        # Mocked gates are for unit tests ONLY, never a release certificate.
        monkeypatch.setattr(
            dispatch, "require_certified_transport_permit",
            lambda permit: permit,
        )
        monkeypatch.setattr(
            dispatch, "assess_strategy_parity_report",
            lambda report: {
                "strategy_parity_verified": True,
                "live_authorization": False,
            },
        )

    intent = EntryIntent(
        signal_id=42, symbol="SIREN_USDT", reference_price=D("100"),
        notional_usdt=D("100"), volume_contracts=D("1"),
        stop_price=D("175"), external_oid="t100v1e" + "a" * 20,
    )
    adm = V40Admission(
        action="ADMIT", reason="modeled_single_bar_admission",
        modeled_notional_usdt=150.0, desired_notional_usdt=150.0,
    )
    plan = FourSlotPlan(
        action="ADMIT", reason=adm.reason,
        occupied_slots=0, modeled_notional_usdt=150.0,
        desired_notional_usdt=150.0, v40_admission=adm,
        candidate_symbol=intent.symbol,
    )
    risk = dispatch.V40CurrentRiskEvidence(
        current_account_equity_usdt=D("1000"),
        available_margin_usdt=D("500"),
        portfolio_notional_usdt=D("0"),
        realized_today_loss_usdt=D("0"),
        current_owned_positions=0,
        signal_age_seconds=30.0,
        book_age_ms=100,
        observed_sell_vwap=D("100"),
        inventory_bookended_complete=True,
        no_unowned_positions_or_orders=True,
        cross_collateral_verified=True,
        private_risk_tier_verified=True,
        contract_trading_access_verified=True,
        hedge_mode_verified=True,
        no_unresolved_mutations=True,
    )
    caps = LiveCaps(
        max_order_usdt=D("200"),
        max_portfolio_usdt=D("500"),
        max_daily_loss_usdt=D("200"),
        max_slippage_bps=D("50"),
    )
    permit = SimpleNamespace(
        max_position_notional_usdt=D("200"),
        max_total_notional_usdt=D("500"),
        max_daily_loss_usdt=D("200"),
        leverage=1,
    )
    class Claims:
        def __init__(self):
            self.calls = 0
            self.block = False
            self.issued = False
        async def claim(self, *, stage, lease):
            self.calls += 1
            if self.block or self.issued:
                raise ConnectionError("claim might already be committed")
            self.issued = True
            return FourSlotDispatchClaim(
                signal_id=stage.signal_id, external_oid=stage.external_oid,
            )
    class Journal:
        def __init__(self):
            self.calls = 0
            self.fail = False
        async def latch_unknown(self, **kw):
            self.calls += 1
            if self.fail:
                raise RuntimeError("DB failed")
    class Transport:
        def __init__(self):
            self.open_type=2
            self.posts=0
            self.fail=False
            self.answer="123456"
        async def submit_short_with_stop(self, intent, *, permit):
            self.posts += 1
            if self.fail:
                raise TimeoutError("ACK might be lost")
            return self.answer

    args = dict(
        plan=plan, intent=intent,
        stage=DurableAttemptStage(
            signal_id=42, external_oid=intent.external_oid,
        ),
        lease=LeaderLease("t100-live-executor", "worker-id", 5, 30),
        risk=risk, caps=caps, permit=permit, price_tick=D("0.1"),
        parity_report={},
        claims=Claims(), journal=Journal(), transport=Transport(),
    )
    return args


def go(args):
    return asyncio.run(dispatch.dispatch_one_staged_v40(**args))


def test_shipped_uncertified_permit_blocks_before_claim_or_post(monkeypatch):
    args = setup(monkeypatch, certify=False)
    with pytest.raises(Exception):
        go(args)
    assert args["claims"].calls == 0
    assert args["transport"].posts == 0


def test_clean_mocked_submission_is_one_post_and_requires_get_reconciliation(monkeypatch):
    args = setup(monkeypatch)
    result = go(args)
    assert result.status == "V40_SINGLE_POST_ACK_UNPROTECTED_RECONCILE_ONLY"
    assert result.post_attempted is True
    assert result.must_reconcile_external_oid is True
    assert result.entry_allowed is False
    assert result.live_authorization is False
    assert result.additional_post_allowed is False
    assert args["claims"].calls == args["transport"].posts == 1
    assert args["journal"].calls == 0
    with pytest.raises(dispatch.V40DispatchHalted, match="CLAIM_UNKNOWN_NO_RETRY"):
        go(args)
    assert args["transport"].posts == 1


@pytest.mark.parametrize("changed,code", [
    ({"plan": "not-a-plan"}, "V40_DISPATCH_ORIGIN_OR_CROSS_MODE_INVALID"),
    ({"stage": DurableAttemptStage(signal_id=99, external_oid="other")},
        "V40_DISPATCH_ORIGIN_OR_CROSS_MODE_INVALID"),
    ({"price_tick": D("0.3")}, "V40_FROZEN_STOP_OR_MODELED_NOTIONAL_MISMATCH"),
    ({"risk": None}, "V40_DISPATCH_ORIGIN_OR_CROSS_MODE_INVALID"),
])
def test_invalid_preclaim_evidence_never_claims_or_posts(monkeypatch, changed, code):
    args = setup(monkeypatch)
    args.update(changed)
    with pytest.raises(dispatch.V40DispatchHalted, match=code):
        go(args)
    assert args["claims"].calls == 0
    assert args["transport"].posts == 0


@pytest.mark.parametrize("risk_field", [
    "inventory_bookended_complete",
    "no_unowned_positions_or_orders",
    "cross_collateral_verified",
    "private_risk_tier_verified",
    "contract_trading_access_verified",
    "hedge_mode_verified",
    "no_unresolved_mutations",
])
def test_missing_private_risk_gate_never_claims(monkeypatch, risk_field):
    args = setup(monkeypatch)
    args["risk"] = replace(args["risk"], **{risk_field: False})
    with pytest.raises(dispatch.V40DispatchHalted,
                       match="V40_ACCOUNT_RISK_OR_FRESHNESS_NOT_CERTIFIED"):
        go(args)
    assert args["claims"].calls == 0


def test_frozen_75pct_stop_cannot_be_tight_btc_experiment(monkeypatch):
    args=setup(monkeypatch)
    args["intent"]=replace(args["intent"], stop_price=D("100.05"))
    with pytest.raises(dispatch.V40DispatchHalted,
                       match="V40_FROZEN_STOP_OR_MODELED_NOTIONAL_MISMATCH"):
        go(args)
    assert args["transport"].posts == 0


def test_operator_daily_loss_budget_blocks_before_claim(monkeypatch):
    args=setup(monkeypatch)
    args["permit"].max_daily_loss_usdt=D("10")
    with pytest.raises(dispatch.V40DispatchHalted,
                       match="V40_MODELED_STOP_EXCEEDS_DAILY_LOSS_CAP"):
        go(args)
    assert args["claims"].calls == 0


def test_wrong_margin_mode_never_claims(monkeypatch):
    args=setup(monkeypatch)
    args["transport"].open_type=1
    with pytest.raises(dispatch.V40DispatchHalted,
                       match="V40_DISPATCH_ORIGIN_OR_CROSS_MODE_INVALID"):
        go(args)
    assert args["transport"].posts == 0


def test_claim_failure_is_ambiguous_and_blocks_order(monkeypatch):
    args=setup(monkeypatch)
    args["claims"].block=True
    with pytest.raises(dispatch.V40DispatchHalted, match="CLAIM_UNKNOWN_NO_RETRY"):
        go(args)
    assert args["transport"].posts == 0


def test_post_timeout_latches_unknown_without_retry(monkeypatch):
    args=setup(monkeypatch)
    args["transport"].fail=True
    with pytest.raises(dispatch.V40DispatchHalted,
                       match="V40_POST_OUTCOME_UNKNOWN_NO_RETRY"):
        go(args)
    assert args["transport"].posts == 1
    assert args["journal"].calls == 1
    with pytest.raises(dispatch.V40DispatchHalted, match="CLAIM_UNKNOWN_NO_RETRY"):
        go(args)
    assert args["transport"].posts == 1


def test_unknown_latch_db_failure_remains_no_retry(monkeypatch):
    args=setup(monkeypatch)
    args["transport"].fail=True
    args["journal"].fail=True
    with pytest.raises(dispatch.V40DispatchHalted,
                       match="UNKNOWN_LATCH_FAILED_NO_RETRY"):
        go(args)
    assert args["transport"].posts == 1
    assert args["journal"].calls == 1


def test_malformed_http_ack_is_not_an_order_confirmation(monkeypatch):
    args=setup(monkeypatch)
    args["transport"].answer="0"
    with pytest.raises(dispatch.V40DispatchHalted,
                       match="V40_POST_OUTCOME_UNKNOWN_NO_RETRY"):
        go(args)
    assert args["transport"].posts == 1
    assert args["journal"].calls == 1

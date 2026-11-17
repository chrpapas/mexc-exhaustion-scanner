"""V40 3x CROSS leverage stays independent from frozen notional and canary.

All exchange POST, active permits, and durable SQL operations are synthetic.
These tests NEVER enable production release flags or access real account data.
"""
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import patch
import asyncio
import pytest

from app.t100_live_execution import LiveCaps
from app.t100_live_authorization import V40_PORTFOLIO_SCOPE
from app.t100_live_mexc_adapter import LiveMexcAdapter
from app.t100_v40_exchange_leverage_plan import (
    V40CrossLeverageInputs,assess_exact_v40_cross_leverage,
)
from app import t100_live_v40_dispatch_executor as dispatch
from tests.test_t100_live_v40_dispatch_executor import setup,go


def portfolio_case(monkeypatch):
    args=setup(monkeypatch)
    cap=LiveCaps(
        max_order_usdt=D("200"),
        max_portfolio_usdt=D("500"),
        max_daily_loss_usdt=D("200"),
        max_slippage_bps=D("50"),
        leverage=3,margin_type=2,
        risk_profile=V40_PORTFOLIO_SCOPE,
    )
    witness=V40CrossLeverageInputs(
        signal_id=42,symbol="SIREN_USDT",
        frozen_lot_notional_usdt=D("100"),
        executable_intent_notional_usdt=D("100"),
        current_gross_notional_usdt=D("0"),
        account_equity_usdt=D("1000"),
        signed_available_initial_margin_usdt=D("45"),
        independent_fee_reserve_usdt=D("1"),
        required_remaining_margin_headroom_usdt=D("10"),
        observed_contract_max_cross_leverage=5,
        observed_account_max_cross_leverage=5,
        operator_max_cross_leverage=5,
        account_cross_mode_verified=True,
        account_identity_bookended_verified=True,
        available_margin_private_signed_get_verified=True,
        per_contract_and_private_leverage_tiers_verified=True,
        frozen_strategy_lot_and_signal_verified=True,
    )
    review=assess_exact_v40_cross_leverage(witness)
    assert review.selected_exchange_cross_leverage==3
    args["caps"]=cap
    args["permit"]=SimpleNamespace(
        max_position_notional_usdt=D("200"),
        max_total_notional_usdt=D("500"),
        max_daily_loss_usdt=D("200"),
        leverage=3,scope=V40_PORTFOLIO_SCOPE,signal_id=42,
        symbol="SIREN_USDT",
    )
    args["risk"]=replace(
        args["risk"],available_margin_usdt=D("45"),
        cross_leverage_plan=review,
    )
    return args


def test_canary_caps_cannot_be_upgraded_by_leverage_field_alone():
    with pytest.raises(ValueError,match="tiny pilot"):
        LiveCaps(
            max_order_usdt=D("200"),max_portfolio_usdt=D("500"),
            max_daily_loss_usdt=D("200"),leverage=3,
        )
    with pytest.raises(ValueError,match="outside reviewed"):
        LiveCaps(
            max_order_usdt=D("200"),max_portfolio_usdt=D("500"),
            max_daily_loss_usdt=D("200"),leverage=6,
            risk_profile=V40_PORTFOLIO_SCOPE,
        )


def test_exact_v40_portfolio_3x_plan_reaches_only_mocked_post(monkeypatch):
    args=portfolio_case(monkeypatch)
    report=go(args)
    assert report.status=="V40_SINGLE_POST_ACK_UNPROTECTED_RECONCILE_ONLY"
    assert args["claims"].calls==1
    assert args["transport"].posts==1
    assert args["intent"].notional_usdt==D("100")
    assert args["risk"].cross_leverage_plan.exact_order_notional_usdt==D("100")
    assert args["risk"].available_margin_usdt==D("45")
    assert args["caps"].leverage==args["permit"].leverage==3
    assert report.entry_allowed is False and report.live_authorization is False


@pytest.mark.parametrize("change,code",[
    ("missing_plan","EXACT_LEVERAGE_OR_LOT_UNVERIFIED"),
    ("wrong_leverage","DISPATCH_ORIGIN_OR_CROSS_MODE_INVALID"),
    ("wrong_signal","PERMIT_SIGNAL_MISMATCH"),
    ("wrong_symbol","PERMIT_SIGNAL_MISMATCH"),
    ("wrong_risk_equity","EXACT_LEVERAGE_OR_LOT_UNVERIFIED"),
    ("wrong_order_notional","EXACT_LEVERAGE_OR_LOT_UNVERIFIED"),
])
def test_portfolio_3x_mismatch_halts_before_claim_or_post(monkeypatch,change,code):
    args=portfolio_case(monkeypatch)
    if change=="missing_plan":
        args["risk"]=replace(args["risk"],cross_leverage_plan=None)
    elif change=="wrong_leverage":
        args["permit"].leverage=2
    elif change=="wrong_signal":
        args["permit"].signal_id=99
    elif change=="wrong_symbol":
        args["permit"].symbol="OTHER_USDT"
    elif change=="wrong_risk_equity":
        args["risk"]=replace(args["risk"],current_account_equity_usdt=D("900"))
    elif change=="wrong_order_notional":
        args["risk"]=replace(
            args["risk"],cross_leverage_plan=replace(
                args["risk"].cross_leverage_plan,
                exact_order_notional_usdt=D("99"),
            ),
        )
    with pytest.raises(dispatch.V40DispatchHalted,match=code):
        go(args)
    assert args["claims"].calls==0
    assert args["transport"].posts==0


def test_scoped_portfolio_permit_cannot_ever_use_legacy_live_gateway():
    # Scope enforcement is separate from nominal margin feasibility.
    from app.t100_live_execution import LiveOrderGateway
    async def run():
        class FakeLedger:pass
        class FakeExchange:open_type=2
        args=portfolio_case_dummy()
        g=LiveOrderGateway(FakeLedger(),FakeExchange(),args["caps"])
        with patch("app.t100_live_execution.require_certified_transport_permit",
                   lambda x:x):
            with pytest.raises(
                Exception,match="V40_PORTFOLIO_LEGACY_ORDER_GATEWAY_FORBIDDEN",
            ):
                await g.send_one(
                    args["intent"],permit=args["permit"],
                    equity_usdt=D("1000"),portfolio_notional=D("0"),
                    today_loss_usdt=D("0"),observed_vwap=D("100"),
                    book_age_ms=100,signal_age_seconds=30,
                )
    asyncio.run(run())


def portfolio_case_dummy():
    from app.t100_live_execution import EntryIntent
    return {
        "caps":LiveCaps(
            max_order_usdt=D("200"),max_portfolio_usdt=D("500"),
            max_daily_loss_usdt=D("200"),leverage=3,
            risk_profile=V40_PORTFOLIO_SCOPE,
        ),
        "permit":SimpleNamespace(scope=V40_PORTFOLIO_SCOPE,leverage=3),
        "intent":EntryIntent(
            signal_id=42,symbol="SIREN_USDT",reference_price=D("100"),
            notional_usdt=D("100"),volume_contracts=D("1"),
            stop_price=D("175"),external_oid="t100v1e"+"a"*20,
        ),
    }


def test_real_mexc_adapter_refuses_portfolio_post_until_final_guard_connected():
    from tests.test_t100_live_mexc_adapter import FakeClient
    client=FakeClient()
    adapter=LiveMexcAdapter(client)
    args=portfolio_case_dummy()
    with patch("app.t100_live_mexc_adapter.require_certified_transport_permit",
               lambda x:x):
        with pytest.raises(
            RuntimeError,match="NONFLAT_POST_GUARD_NOT_CONNECTED_NO_POST",
        ):
            asyncio.run(adapter.submit_short_with_stop(
                args["intent"],permit=args["permit"],
            ))
    assert client.posts==[] and client.get_calls==[]

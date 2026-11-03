"""Offline fail-closed one-shot tiny-pilot coordinator tests."""
import unittest
from dataclasses import replace
from decimal import Decimal as D

from app.t100_live_authorization import (
    LivePilotReadinessEvidence,
    issue_live_mutation_permit,
)
from app.t100_live_execution import EntryIntent, LiveCaps
from app.t100_live_guards import LIVE_CONFIRMATION, T100ExecutionControls
from app.t100_live_pilot_coordinator import (
    TinyPilotCoordinator,
    TinyPilotHalted,
    TinyPilotMarketEvidence,
)
from app.t100_v40_admission import V40Admission


def permit():
    controls = T100ExecutionControls.from_env({
        "T100_EXECUTION_MODE": "live",
        "T100_LIVE_ORDER_API_ENABLED": "true",
        "T100_LIVE_KILL_SWITCH": "false",
        "T100_LIVE_OPERATOR_CONFIRM": LIVE_CONFIRMATION,
        "T100_LIVE_MAX_POSITION_NOTIONAL_USDT": "100",
        "T100_LIVE_MAX_TOTAL_NOTIONAL_USDT": "100",
        "T100_LIVE_MAX_DAILY_LOSS_USDT": "25",
        "T100_LIVE_LEVERAGE": "1",
    })
    evidence = LivePilotReadinessEvidence(
        code_revision_verified=True,
        strategy_parity_verified=True,
        durable_schema_deployed_verified=True,
        current_inventory_completeness_verified=True,
        account_reconciled=True,
        protected_position_soak_verified=True,
        restart_recovery_verified=True,
        native_stop_mutation_contract_verified=True,
        entry_reconciliation_verified=True,
        fill_and_accounting_verified=True,
        private_ws_pre_pilot_contract_verified=True,
        unresolved_mutations_absent=True,
        independent_review_completed=True,
        operator_tiny_pilot_authorized=True,
    )
    return issue_live_mutation_permit(controls, evidence)


def intent(notional=D("50")):
    return EntryIntent(
        signal_id=1,
        symbol="SIREN_USDT",
        reference_price=D("100"),
        notional_usdt=notional,
        volume_contracts=D("5"),
        stop_price=D("175"),
        external_oid="t100v1eabc",
    )


def admission(**kw):
    data = dict(
        symbol="SIREN_USDT",
        candidate_ts_ms=1000,
        risk_tier="HIGH",
        status="OK",
        action="ADMIT",
        reasons=(),
        target_fraction=D("0.625"),
        historical_cap_usdt=D("1000"),
        shock_budget_usdt=D("100"),
        modeled_entry_notional=D("100"),
    )
    data.update(kw)
    return V40Admission(**data)


def market(**kw):
    data = dict(
        equity_usdt=D("1000"),
        portfolio_notional_usdt=D("0"),
        today_loss_usdt=D("0"),
        observed_vwap=D("100"),
        book_age_ms=100,
        signal_age_seconds=5.0,
    )
    data.update(kw)
    return TinyPilotMarketEvidence(**data)


def dynamic(**kw):
    data = {
        "dynamic_readiness_verified": True,
        "durable_schema_deployed_verified": True,
        "unresolved_mutations_absent": True,
        "current_inventory_completeness_verified": True,
        "account_reconciled": True,
        "account_reconciliation": {
            "flat_account_reconciled": True,
            "account_reconciled": True,
        },
    }
    data.update(kw)
    return data


class Gateway:
    def __init__(self):
        self.calls = 0
        self.error = None
        self.caps = LiveCaps(D("100"), D("100"), D("25"))

    async def send_one(self, entry, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return "123456"


class Supplier:
    def __init__(self):
        self.report = dynamic()
        self.calls = 0
        self.error = None

    async def __call__(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.report


class PilotCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = Gateway()
        self.supplier = Supplier()
        self.coordinator = TinyPilotCoordinator(
            gateway=self.gateway,
            dynamic_readiness_supplier=self.supplier,
        )
        self.code = {"code_revision_verified": True}
        self.strategy = {"strategy_parity_verified": True}
        self.ws = {
            "private_ws_pre_pilot_contract_verified": True
        }

    async def submit(self, **kw):
        data = dict(
            intent=intent(),
            admission=admission(),
            permit=permit(),
            code_revision_report=self.code,
            strategy_parity_report=self.strategy,
            ws_pre_pilot_report=self.ws,
            market=market(),
        )
        data.update(kw)
        return await self.coordinator.submit_one(**data)

    async def test_all_gates_allow_exactly_one_gateway_attempt(self):
        r = await self.submit()
        self.assertEqual(self.gateway.calls, 1)
        self.assertEqual(self.supplier.calls, 1)
        self.assertEqual(r.order_id, "123456")
        self.assertTrue(r.pilot_notional_reduced)
        self.assertTrue(r.protected_entry_reconciliation_required)
        self.assertFalse(r.live_authorization)

    async def test_static_release_gate_failure_means_zero_dynamic_and_gateway_calls(self):
        cases = (
            ("code_revision_report", {"code_revision_verified": False}),
            ("strategy_parity_report", {"strategy_parity_verified": False}),
            (
                "ws_pre_pilot_report",
                {"private_ws_pre_pilot_contract_verified": False},
            ),
        )
        for field, value in cases:
            self.gateway.calls = 0
            self.supplier.calls = 0
            with self.subTest(field=field):
                with self.assertRaises(TinyPilotHalted):
                    await self.submit(**{field: value})
                self.assertEqual(self.supplier.calls, 0)
                self.assertEqual(self.gateway.calls, 0)

    async def test_non_admitted_or_oversized_intent_blocks_before_dynamic_read(self):
        cases = (
            admission(action="SKIP"),
            admission(symbol="OTHER_USDT"),
        )
        for item in cases:
            self.gateway.calls = 0
            self.supplier.calls = 0
            with self.subTest(item=item):
                with self.assertRaises(TinyPilotHalted):
                    await self.submit(admission=item)
                self.assertEqual(self.supplier.calls, 0)
                self.assertEqual(self.gateway.calls, 0)

        with self.assertRaises(TinyPilotHalted):
            await self.submit(intent=intent(D("101")))
        self.assertEqual(self.gateway.calls, 0)

    async def test_first_pilot_requires_zero_portfolio_notional(self):
        with self.assertRaisesRegex(
            TinyPilotHalted, "ZERO_PORTFOLIO"
        ):
            await self.submit(
                market=market(portfolio_notional_usdt=D("1"))
            )
        self.assertEqual(self.gateway.calls, 0)

    async def test_dynamic_readiness_is_rechecked_immediately_before_gateway(self):
        self.supplier.report = dynamic(
            dynamic_readiness_verified=False
        )
        with self.assertRaisesRegex(
            TinyPilotHalted, "DYNAMIC_READINESS_BLOCKED"
        ):
            await self.submit()
        self.assertEqual(self.supplier.calls, 1)
        self.assertEqual(self.gateway.calls, 0)

    async def test_flat_account_is_mandatory_for_first_pilot(self):
        self.supplier.report = dynamic(
            account_reconciliation={
                "flat_account_reconciled": False,
                "account_reconciled": True,
            }
        )
        with self.assertRaisesRegex(
            TinyPilotHalted, "FLAT_RECONCILED_ACCOUNT"
        ):
            await self.submit()
        self.assertEqual(self.gateway.calls, 0)

    async def test_defensive_dynamic_subgate_cannot_be_false(self):
        for field in (
            "durable_schema_deployed_verified",
            "unresolved_mutations_absent",
            "current_inventory_completeness_verified",
            "account_reconciled",
        ):
            self.gateway.calls = 0
            self.supplier.report = dynamic(**{field: False})
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    TinyPilotHalted, "DYNAMIC_GATE_FALSE"
                ):
                    await self.submit()
                self.assertEqual(self.gateway.calls, 0)

    async def test_gateway_failure_does_not_trigger_second_attempt(self):
        self.gateway.error = TimeoutError("unknown order outcome")
        with self.assertRaisesRegex(
            TinyPilotHalted, "GATEWAY_HALTED_OR_OUTCOME_UNKNOWN"
        ):
            await self.submit()
        self.assertEqual(self.gateway.calls, 1)


if __name__ == "__main__":
    unittest.main()

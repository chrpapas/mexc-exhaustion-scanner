"""Offline contract tests: no credentials, network, or orders."""
from __future__ import annotations

import unittest
from unittest.mock import patch
from decimal import Decimal
from app.t100_live_authorization import (
    LivePilotReadinessEvidence,
    issue_live_mutation_permit,
)
from app.t100_live_execution import EntryIntent, LiveCaps, LiveOrderGateway, ExecutionHalted, displayed_vwap, modeled_short_loss_at_native_stop
from app.t100_live_guards import LIVE_CONFIRMATION, T100ExecutionControls


def permit(max_position="1000", max_total="2000", max_daily_loss="500"):
    controls = T100ExecutionControls.from_env({
        "T100_EXECUTION_MODE": "live",
        "T100_LIVE_ORDER_API_ENABLED": "true",
        "T100_LIVE_KILL_SWITCH": "false",
        "T100_LIVE_OPERATOR_CONFIRM": LIVE_CONFIRMATION,
        "T100_LIVE_MAX_POSITION_NOTIONAL_USDT": max_position,
        "T100_LIVE_MAX_TOTAL_NOTIONAL_USDT": max_total,
        "T100_LIVE_MAX_DAILY_LOSS_USDT": max_daily_loss,
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


def intent():
    return EntryIntent.build(
        signal_id=112, symbol="SIREN_USDT", reference_price="2",
        notional_usdt="500", contract_size="1", volume_step="1",
        min_volume="1", max_volume="1000", price_tick="0.001")


class MemoryLedger:
    def __init__(self):
        self.values, self.history = {}, []
    async def insert_prepared(self, intent):
        if intent.external_oid in self.values:
            return False
        self.values[intent.external_oid] = "PREPARED"
        self.history.append("persist")
        return True
    async def transition(self, oid, expected, target, *, order_id=None, reason=None):
        self.history.append(expected + "->" + target)
        if self.values[oid] != expected:
            return False
        self.values[oid] = target
        return True
    async def state(self, oid):
        return self.values[oid]


class MemoryMexc:
    open_type = 2  # Mock CROSS-only pilot transport.

    def __init__(self):
        self.posts, self.fail, self.order, self.stops, self.positions = 0, False, None, [], []
    async def submit_short_with_stop(self, intent, *, permit):
        self.posts += 1
        if self.fail:
            raise TimeoutError()
        return "98765"
    async def order_by_external_oid(self, symbol, oid):
        return self.order
    async def open_positions(self, symbol=None):
        return self.positions
    async def open_stop_orders(self, symbol=None):
        return self.stops


class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._adapter_patch = patch(
            "app.t100_live_guards.LIVE_ADAPTER_STATUS", "CERTIFIED"
        )
        self._adapter_patch.start()
        self.addCleanup(self._adapter_patch.stop)
        self.intent = intent()
        self.ledger = MemoryLedger()
        self.mexc = MemoryMexc()
        self.gateway = LiveOrderGateway(
            self.ledger, self.mexc,
            LiveCaps(Decimal("1000"), Decimal("2000"), Decimal("500")))
    async def send(self, **overrides):
        data = dict(
            permit=permit(),
            equity_usdt=Decimal("1200"), portfolio_notional=Decimal(0),
            today_loss_usdt=Decimal(0), observed_vwap=Decimal("2"),
            book_age_ms=100, signal_age_seconds=20)
        data.update(overrides)
        return await self.gateway.send_one(self.intent, **data)

    async def test_pilot_cross_margin_transport_is_mandatory(self):
        self.assertEqual(self.gateway.caps.margin_type, 2)
        self.assertEqual(self.mexc.open_type, 2)
        # Legacy isolated payload adapter or missing mode must be blocked
        # BEFORE creating an intent or making any exchange request.
        self.mexc.open_type = 1
        with self.assertRaisesRegex(
            ExecutionHalted, "CROSS_PILOT_MARGIN_TRANSPORT_UNVERIFIED"
        ):
            await self.send()
        self.assertEqual(self.ledger.history, [])
        self.assertEqual(self.mexc.posts, 0)
        del self.mexc.open_type
        with self.assertRaisesRegex(
            ExecutionHalted, "CROSS_PILOT_MARGIN_TRANSPORT_UNVERIFIED"
        ):
            await self.send()
        self.assertEqual(self.ledger.values, {})

    async def test_underbudget_stop_risk_blocks_before_writing_any_intent(self):
        # Frozen 75% SL implies ~375 USDT gross potential loss from
        # a 500 USDT notional short, before friction or gap-through.
        with self.assertRaisesRegex(
            ExecutionHalted, "PROSPECTIVE_STOP_LOSS_EXCEEDS_DAILY_BUDGET"
        ):
            await self.send(permit=permit(max_daily_loss="100"))
        self.assertEqual(self.mexc.posts, 0)
        self.assertEqual(self.ledger.history, [])
        self.gateway.caps = LiveCaps(
            Decimal("1000"), Decimal("2000"), Decimal("100")
        )
        with self.assertRaisesRegex(
            ExecutionHalted, "PROSPECTIVE_STOP_LOSS_EXCEEDS_DAILY_BUDGET"
        ):
            await self.send()
        self.assertEqual(self.mexc.posts, 0)
        self.assertEqual(self.ledger.values, {})

    async def test_small_one_signal_canary_under_budget_with_mock_transport(self):
        # This simulates a mock POST in a unit test with a PATCHED adapter.
        # In production adapter remains NOT_IMPLEMENTED, never authorized.
        small = EntryIntent.build(
            signal_id=113, symbol="SIREN_USDT",
            reference_price="2", notional_usdt="10",
            contract_size="1", volume_step="1",
            min_volume="1", max_volume="1000", price_tick="0.001",
        )
        modeled = modeled_short_loss_at_native_stop(
            small, max_adverse_slippage_bps=Decimal("100")
        )
        self.assertGreater(modeled, Decimal("7.50"))
        self.assertLess(modeled, Decimal("10"))
        result = await self.gateway.send_one(
            small, permit=permit(max_daily_loss="10"),
            equity_usdt=Decimal("1200"),
            portfolio_notional=Decimal(0),
            today_loss_usdt=Decimal(0),
            observed_vwap=Decimal("2"),
            book_age_ms=100, signal_age_seconds=20,
        )
        self.assertEqual(result, "98765")
        self.assertEqual(self.mexc.posts, 1)
        self.assertIn(small.external_oid, self.ledger.values)

    async def test_prospective_loss_plus_existing_loss_must_fit_cap(self):
        small = EntryIntent.build(
            signal_id=114, symbol="SIREN_USDT",
            reference_price="2", notional_usdt="10",
            contract_size="1", volume_step="1",
            min_volume="1", max_volume="1000", price_tick="0.001",
        )
        with self.assertRaisesRegex(
            ExecutionHalted, "PROSPECTIVE_STOP_LOSS_EXCEEDS_DAILY_BUDGET"
        ):
            await self.gateway.send_one(
                small, permit=permit(max_daily_loss="10"),
                equity_usdt=Decimal("1200"),
                portfolio_notional=Decimal(0),
                today_loss_usdt=Decimal("3"),
                observed_vwap=Decimal("2"),
                book_age_ms=100, signal_age_seconds=20,
            )
        self.assertEqual(self.mexc.posts, 0)
        self.assertEqual(self.ledger.values, {})

    async def test_uncertified_adapter_blocks_before_ledger_and_post(self):
        with patch("app.t100_live_guards.LIVE_ADAPTER_STATUS", "NOT_IMPLEMENTED"):
            with self.assertRaisesRegex(Exception, "LIVE_ADAPTER_NOT_CERTIFIED"):
                await self.send()
        self.assertEqual(self.ledger.values, {})
        self.assertEqual(self.mexc.posts, 0)

    async def test_permit_caps_block_before_order(self):
        with self.assertRaisesRegex(ExecutionHalted, "PERMIT_ORDER_CAP"):
            await self.send(permit=permit(max_position="400"))
        self.assertEqual(self.mexc.posts, 0)

    async def test_invalid_permit_blocks_before_order(self):
        with self.assertRaises(Exception):
            await self.send(permit=None)
        self.assertEqual(self.mexc.posts, 0)

    async def test_persist_before_post(self):
        self.assertEqual(await self.send(), "98765")
        self.assertEqual(self.ledger.history[:2], ["persist", "PREPARED->SUBMITTING"])
    async def test_duplicate_no_retry(self):
        await self.send()
        with self.assertRaises(ExecutionHalted):
            await self.send()
        self.assertEqual(self.mexc.posts, 1)
    async def test_timeout_unknown_no_retry(self):
        self.mexc.fail = True
        with self.assertRaisesRegex(ExecutionHalted, "SUBMISSION_UNKNOWN"):
            await self.send()
        self.assertEqual(self.ledger.values[self.intent.external_oid], "UNKNOWN")
        with self.assertRaises(ExecutionHalted):
            await self.send()
        self.assertEqual(self.mexc.posts, 1)
    async def test_absent_order_never_resubmits(self):
        self.ledger.values[self.intent.external_oid] = "UNKNOWN"
        with self.assertRaisesRegex(ExecutionHalted, "NO_ORDER_FOUND"):
            await self.gateway.reconcile(self.intent)
        self.assertEqual(self.mexc.posts, 0)
    async def test_native_stop_required(self):
        self.ledger.values[self.intent.external_oid] = "ACK"
        self.mexc.order = dict(orderId="98765", symbol=self.intent.symbol,
                               externalOid=self.intent.external_oid, side=3,
                               state=3, dealVol="250", positionId="199")
        with self.assertRaisesRegex(ExecutionHalted, "UNPROTECTED"):
            await self.gateway.reconcile(self.intent)
        self.mexc.stops = [dict(positionId="199", state=1, stopLossPrice="3.5", vol="250")]
        self.assertEqual(await self.gateway.reconcile(self.intent), "VERIFIED_PROTECTED")

    async def test_entire_position_same_stop_reconciles_without_explicit_volume(self):
        self.ledger.values[self.intent.external_oid] = "ACK"
        self.mexc.order = dict(
            orderId="98765",
            symbol=self.intent.symbol,
            externalOid=self.intent.external_oid,
            side=3,
            state=3,
            dealVol="250",
            positionId="199",
        )
        self.mexc.stops = [dict(
            positionId="199",
            state=1,
            stopLossPrice="3.5",
            vol="0",
            volType=2,
            profitLossVolType="SAME",
        )]
        self.assertEqual(
            await self.gateway.reconcile(self.intent),
            "VERIFIED_PROTECTED",
        )
    async def test_gates_reject_before_order(self):
        for kw in (dict(book_age_ms=1501), dict(signal_age_seconds=901),
                   dict(today_loss_usdt=Decimal("500")),
                   dict(observed_vwap=Decimal("1.96")),
                   dict(equity_usdt=Decimal("100"))):
            with self.subTest(kw=kw), self.assertRaises(ExecutionHalted):
                await self.send(**kw)
        self.assertEqual(self.mexc.posts, 0)


class MathTests(unittest.TestCase):
    def test_frozen_short_stop_loss_includes_round_trip_friction(self):
        observed = modeled_short_loss_at_native_stop(
            intent(), max_adverse_slippage_bps=Decimal("100")
        )
        self.assertEqual(observed, Decimal("390.125"))

    def test_invalid_or_too_close_stop_fails_closed(self):
        from dataclasses import replace
        with self.assertRaises(ExecutionHalted):
            modeled_short_loss_at_native_stop(
                replace(intent(), stop_price=Decimal("2.2")),
                max_adverse_slippage_bps=Decimal("100"),
            )
        with self.assertRaises(ExecutionHalted):
            modeled_short_loss_at_native_stop(
                intent(), max_adverse_slippage_bps=Decimal("NaN")
            )

    def test_integer_contracts(self):
        self.assertEqual(intent().volume_contracts, Decimal("250"))
    def test_depth_uses_contracts_not_coins(self):
        px, qty = displayed_vwap(levels=[["1.99", "100"], ["1.98", "150"]],
            contract_size=Decimal("10"), requested_contracts=Decimal("200"), side="sell")
        self.assertEqual(px, Decimal("1.985"))
        self.assertEqual(qty, Decimal("200"))
    def test_no_btc_eth(self):
        for symbol in ("BTC_USDT", "ETH_USDT"):
            with self.assertRaises(ValueError):
                EntryIntent.build(signal_id=1,symbol=symbol,reference_price="2",
                    notional_usdt="100",contract_size="1",volume_step="1",
                    min_volume="1",max_volume="1000",price_tick="0.01")
    def test_pilot_margin_mode_cannot_be_isolated(self):
        with self.assertRaisesRegex(ValueError, "CROSS"):
            LiveCaps(
                Decimal("100"), Decimal("200"), Decimal("50"),
                margin_type=1,
            )

    def test_leverage_is_one(self):
        with self.assertRaises(ValueError):
            LiveCaps(Decimal("100"), Decimal("200"), Decimal("10"), leverage=2)


if __name__=="__main__":
    unittest.main()

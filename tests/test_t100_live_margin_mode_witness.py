"""Offline, no-network MEXC private margin-mode corroboration tests."""
from __future__ import annotations

import copy
import unittest

from app.t100_live_margin_mode_witness import inspect_margin_mode
from app.t100_live_margin_mode_witness_cli import run
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


class Client:
    def __init__(self, *, risk_mode=1, leverage_mode=1,
                 second_risk_mode=None, second_leverage_mode=None,
                 no_short=False, bad_scope=False, no_leverage=False):
        self.risk_modes = [risk_mode, risk_mode if second_risk_mode is None
                           else second_risk_mode]
        self.leverage_modes = [leverage_mode, leverage_mode if second_leverage_mode is None
                               else second_leverage_mode]
        self.no_short = no_short
        self.bad_scope = bad_scope
        self.no_leverage = no_leverage
        self.calls = []
        self.risk_count = 0
        self.leverage_count = 0

    async def _private_get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == "/api/v1/private/account/risk_limit":
            mode = self.risk_modes[self.risk_count]
            self.risk_count += 1
            symbol = "OTHER_USDT" if self.bad_scope else "TEST_USDT"
            return {symbol: [{
                "symbol": symbol, "positionType": 1 if self.no_short else 2,
                "level": 1, "openType": mode, "maxVol": 100,
                "maxLeverage": 10, "mmr": 0.01, "imr": 0.02,
                "currentMmr": 0.01, "leverage": 5,
            }]}
        if path == "/api/v1/private/position/leverage":
            if self.no_leverage:
                raise RuntimeError("SECRET_PRIVATE_RESPONSE")
            mode = self.leverage_modes[self.leverage_count]
            self.leverage_count += 1
            # The documented response may omit symbol, despite request scope.
            return [{
                "positionType": 1 if self.no_short else 2,
                "openType": mode, "leverage": 5,
                "currentMmr": 0.01, "maxLeverageView": 10,
            }]
        raise AssertionError("UNEXPECTED_ENDPOINT")


class MarginModeWitnessTests(unittest.IsolatedAsyncioTestCase):
    async def witness(self, client):
        return await inspect_margin_mode(
            ReadOnlyMexcProbe(client), client, "TEST_USDT",
        )

    async def test_isolated_agrees_without_trade_permission(self):
        client = Client(risk_mode=1, leverage_mode=1)
        result = await self.witness(client)
        self.assertEqual(result.status,
                         "MARGIN_MODE_CURRENT_ISOLATED_CORROBORATED_NONAUTHORIZING")
        self.assertEqual(result.risk_limit_mode, "ISOLATED")
        self.assertEqual(result.leverage_endpoint_mode, "ISOLATED")
        self.assertTrue(result.independent_mode_reads_agree)
        self.assertTrue(result.risk_limit_two_get_consistent)
        self.assertTrue(result.leverage_two_get_consistent)
        self.assertFalse(result.private_cross_mode_corroborated)
        self.assertFalse(result.candidate_contract_sizing_verified)
        self.assertFalse(result.account_wide_collateral_certified)
        self.assertFalse(result.position_ownership_verified)
        self.assertFalse(result.native_stop_protection_verified)
        self.assertFalse(result.venue_liquidation_certified)
        self.assertFalse(result.entry_allowed)
        self.assertFalse(result.live_authorization)
        self.assertFalse(result.mexc_orders_sent)
        self.assertEqual(len(client.calls), 4)
        self.assertEqual([x[0] for x in client.calls], [
            "/api/v1/private/account/risk_limit",
            "/api/v1/private/position/leverage",
            "/api/v1/private/account/risk_limit",
            "/api/v1/private/position/leverage",
        ])
        self.assertTrue(all(x[1] == {"symbol": "TEST_USDT"}
                            for x in client.calls))
        self.assertNotIn("TEST_USDT", str(result))

    async def test_cross_agrees_but_not_authorized(self):
        result = await self.witness(Client(risk_mode=2, leverage_mode=2))
        self.assertEqual(result.status,
                         "MARGIN_MODE_CURRENT_CROSS_CORROBORATED_NONAUTHORIZING")
        self.assertTrue(result.private_cross_mode_corroborated)
        self.assertFalse(result.entry_allowed)

    async def test_mode_mismatch_rejected(self):
        result = await self.witness(Client(risk_mode=1, leverage_mode=2))
        self.assertEqual(result.status, "MARGIN_MODE_ENDPOINT_DISAGREEMENT")
        self.assertFalse(result.independent_mode_reads_agree)
        self.assertFalse(result.private_cross_mode_corroborated)

    async def test_both_endpoint_drift_rejected(self):
        for client in (
            Client(risk_mode=1, second_risk_mode=2),
            Client(leverage_mode=1, second_leverage_mode=2),
        ):
            result = await self.witness(client)
            self.assertEqual(result.status, "MARGIN_MODE_CRITICAL_READ_DRIFT")
            self.assertFalse(result.entry_allowed)

    async def test_unknown_mode_cannot_pass(self):
        for client in (Client(risk_mode=None), Client(leverage_mode=0)):
            result = await self.witness(client)
            self.assertEqual(result.status, "MARGIN_MODE_VALUE_UNVERIFIED")
            self.assertFalse(result.independent_mode_reads_agree)

    async def test_missing_short_or_wrong_scope_blocked(self):
        for client in (Client(no_short=True), Client(bad_scope=True)):
            result = await self.witness(client)
            self.assertEqual(result.status, "MARGIN_MODE_RISK_GET_OR_SCHEMA_FAILED")

    async def test_leverage_get_failure_censored(self):
        result = await self.witness(Client(no_leverage=True))
        self.assertEqual(result.status, "MARGIN_MODE_LEVERAGE_GET_FAILED")
        self.assertNotIn("SECRET", str(result))

    async def test_invalid_symbol_never_reads_account(self):
        client = Client()
        result = await inspect_margin_mode(
            ReadOnlyMexcProbe(client), client, "../bad",
        )
        self.assertEqual(result.status, "MARGIN_MODE_SYMBOL_INVALID")
        self.assertEqual(client.calls, [])

    async def test_cli_requires_explicit_paper_kill_before_credentials(self):
        for env in ({}, {
            "T100_EXECUTION_MODE": "live",
            "T100_LIVE_ORDER_API_ENABLED": "false",
            "T100_LIVE_KILL_SWITCH": "true",
        }):
            result = await run("TEST_USDT", environ=env)
            self.assertEqual(result["status"], "MARGIN_MODE_PAPER_KILL_REQUIRED")
            self.assertFalse(result["mexc_orders_sent"])
        result = await run("TEST_USDT", environ={
            "T100_EXECUTION_MODE": "paper",
            "T100_LIVE_ORDER_API_ENABLED": "false",
            "T100_LIVE_KILL_SWITCH": "true",
        })
        self.assertEqual(result["status"], "MARGIN_MODE_CREDENTIALS_MISSING")


if __name__ == "__main__":
    unittest.main()

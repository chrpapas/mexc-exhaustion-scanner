"""Preflight regression tests: injected fake client, no network or real orders."""
import os
import unittest
from unittest.mock import patch

from app.t100_live_preflight import read_only_preflight


class FakeClient:
    def __init__(self, positions=None, stops=None):
        self.positions = [] if positions is None else positions
        self.stops = [] if stops is None else stops
        self.calls = []
        self._spec_cache = {"SIREN_USDT": object()}

    async def ping(self):
        return True

    async def usdt_asset(self):
        return {"equity": 100}

    async def usdt_available_balance(self):
        return 100.0

    async def position_mode(self):
        return 1

    async def refresh_contract_specs(self):
        return None

    async def _private_get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/api/v1/private/position/open_positions":
            return self.positions
        if path == "/api/v1/private/stoporder/open_orders":
            return self.stops
        raise AssertionError("UNEXPECTED_PRIVATE_READ")


class PreflightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {
            "MEXC_API_KEY": "FAKE_ONLY",
            "MEXC_API_SECRET": "FAKE_ONLY",
            "T100_EXECUTION_MODE": "paper",
        })
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()

    async def test_empty_inventory_is_still_uncertified(self):
        c = FakeClient()
        result = await read_only_preflight(client=c)
        self.assertEqual(result["open_futures_positions"], 0)
        self.assertEqual(result["open_futures_stop_orders"], 0)
        self.assertIn("venue_read_contract_and_pagination_not_certified",
                      result["preflight_failures"])
        self.assertFalse(result["live_order_submission_possible"])
        self.assertEqual(len(c.calls), 2)
        self.assertTrue(result["redacted_inventory_schema"]["positions"]["empty_rows_cannot_establish_schema"])

    async def test_schema_evidence_redacts_account_values(self):
        c = FakeClient(positions=[{"positionId": "PRIVATE-123",
                                   "symbol": "SIREN_USDT",
                                   "unknownSecretField": "SECRET-XYZ"}],
                       stops=[{"id": "PRIVATE-STOP-999"}])
        result = await read_only_preflight(client=c)
        schema = str(result["redacted_inventory_schema"])
        for secret in ("PRIVATE-123", "SIREN_USDT", "SECRET-XYZ",
                       "unknownSecretField", "PRIVATE-STOP-999"):
            self.assertNotIn(secret, schema)
        self.assertEqual(result["redacted_inventory_schema"]["positions"]["unlisted_field_occurrences"], 1)

    async def test_nonempty_positions_block(self):
        result = await read_only_preflight(
            client=FakeClient(positions=[{"positionId": "10"}]))
        self.assertIn("account_has_existing_positions", result["preflight_failures"])

    async def test_orphan_stop_blocks(self):
        result = await read_only_preflight(
            client=FakeClient(stops=[{"id": "99"}]))
        self.assertIn("account_has_existing_stop_orders", result["preflight_failures"])

    async def test_malformed_positions_cannot_look_flat(self):
        result = await read_only_preflight(
            client=FakeClient(positions=[{"positionId": "10"}, None]))
        self.assertIn("private_or_public_read_failed", result["preflight_failures"])
        self.assertNotIn("open_futures_positions", result)

    async def test_malformed_stops_cannot_look_flat(self):
        result = await read_only_preflight(
            client=FakeClient(stops=[{"id": "99"}, None]))
        self.assertIn("private_or_public_read_failed", result["preflight_failures"])
        self.assertNotIn("open_futures_stop_orders", result)

    async def test_null_positions_fail(self):
        c = FakeClient()
        c.positions = None
        result = await read_only_preflight(client=c)
        self.assertIn("private_or_public_read_failed", result["preflight_failures"])

    async def test_null_stops_fail(self):
        c = FakeClient()
        c.stops = None
        result = await read_only_preflight(client=c)
        self.assertIn("private_or_public_read_failed", result["preflight_failures"])

    async def test_missing_credentials_makes_no_calls(self):
        with patch.dict(os.environ, {"MEXC_API_KEY": "", "MEXC_API_SECRET": ""}):
            c = FakeClient()
            result = await read_only_preflight(client=c)
            self.assertEqual(c.calls, [])
            self.assertIn("MISSING", result["credential_status"])

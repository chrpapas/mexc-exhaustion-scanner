"""Preflight regression tests: injected fake client, no network or real orders."""
import os
import unittest
from unittest.mock import patch

from app.t100_live_preflight import read_only_preflight, shareable_contract_report


class FakeClient:
    def __init__(self, positions=None, stops=None, stop_history=None):
        self.positions = [] if positions is None else positions
        self.stops = [] if stops is None else stops
        self.stop_history = [] if stop_history is None else stop_history
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
        if path == "/api/v1/private/stoporder/list/orders":
            if params != {"page_num": 1, "page_size": 100, "is_finished": 1}:
                raise AssertionError("UNEXPECTED_STOP_HISTORY_PARAMS")
            return self.stop_history
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
        self.assertEqual(result["shadow_schema_assessment"]["overall"], "INCOMPLETE_SCHEMA_EVIDENCE")

    async def test_schema_only_history_can_fill_stop_shape_without_certifying_pagination(self):
        c = FakeClient(
            positions=[{"positionId": 10, "symbol": "SIREN_USDT",
                        "positionType": 2, "holdVol": 1}],
            stop_history=[{"id": 99, "positionId": 10, "state": 3,
                           "stopLossPrice": 1.25, "vol": 1}],
        )
        result = await read_only_preflight(
            client=c, include_historical_stop_schema=True)
        self.assertEqual(result["stop_schema_source"], "historical_finished_page1")
        self.assertEqual(
            result["shadow_schema_assessment"]["overall"],
            "PARSER_SHAPE_COMPATIBLE_UNCERTIFIED")
        self.assertFalse(result["shadow_schema_assessment"]["pagination_verified"])
        self.assertEqual(len(c.calls), 3)

    async def test_empty_history_does_not_create_false_stop_schema(self):
        result = await read_only_preflight(
            client=FakeClient(), include_historical_stop_schema=True)
        self.assertEqual(result["stop_schema_source"], "open_orders")
        self.assertEqual(
            result["shadow_schema_assessment"]["overall"],
            "INCOMPLETE_SCHEMA_EVIDENCE")

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

    async def test_shareable_report_never_includes_account_values(self):
        result = await read_only_preflight(client=FakeClient())
        result["usdt_equity"] = 987654321.12
        result["api_key"] = "DO_NOT_SHARE"
        shared = shareable_contract_report(result)
        self.assertNotIn("987654321", str(shared))
        self.assertNotIn("DO_NOT_SHARE", str(shared))
        self.assertFalse(shared["venue_contract_certified"])
        self.assertFalse(shared["pagination_certified"])
        self.assertEqual(shared["shadow_schema_assessment"]["overall"], "INCOMPLETE_SCHEMA_EVIDENCE")

    async def test_missing_credentials_makes_no_calls(self):
        with patch.dict(os.environ, {"MEXC_API_KEY": "", "MEXC_API_SECRET": ""}):
            c = FakeClient()
            result = await read_only_preflight(client=c)
            self.assertEqual(c.calls, [])
            self.assertIn("MISSING", result["credential_status"])

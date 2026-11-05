"""Tests for the opt-in, GET-and-SELECT only guardian CLI.

No real network, no exchange mutations, no database writes, no Render.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from urllib.parse import urlencode

from app.t100_live_guardian_readonly_once import (
    GetOnlyMexcTransport, GuardianOnceHalted, inspect_once,
    validate_once_config,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe

from tests.test_t100_live_guardian_checkpoint_reader import (
    KEY, SCOPE, FakeDb,
)
from tests.test_t100_live_guardian_observer import Venue


def env(**changes):
    item=dict(
        T100_GUARDIAN_OBSERVER_ENABLED="true",
        T100_EXECUTION_MODE="paper",
        T100_LIVE_ORDER_API_ENABLED="false",
        T100_LIVE_KILL_SWITCH="true",
        T100_GUARDIAN_MEXC_API_KEY=KEY,
        T100_GUARDIAN_MEXC_API_SECRET="test-not-real-api-secret-12345678",
        T100_GUARDIAN_ACCOUNT_KEY_SHA256=SCOPE,
        T100_GUARDIAN_POSITION_ID="555",
        T100_GUARDIAN_READONLY_DB_DSN="postgresql://reader@db.example/readonly",
        T100_GUARDIAN_DB_CA_FILE="/path/to/ca.pem",
    )
    item.update(changes)
    return item


class MockResponse:
    def __init__(self,payload=None):
        self.payload = (payload if payload is not None
                        else dict(success=True,code=0,data=[]))

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class HTTP:
    def __init__(self,response=None):
        self.calls = []
        self.response = response or MockResponse()

    async def get(self,path,**kwargs):
        self.calls.append((path,kwargs))
        return self.response


class ConfigTests(unittest.TestCase):
    def test_good_config_requires_explicit_scope_and_role_connection(self):
        item=validate_once_config(env())
        self.assertEqual(item["scope"],SCOPE)
        self.assertEqual(item["position_id"],"555")

    def test_default_and_live_mode_cannot_activate(self):
        for changes in (
            dict(T100_GUARDIAN_OBSERVER_ENABLED="false"),
            dict(T100_EXECUTION_MODE="live"),
            dict(T100_LIVE_ORDER_API_ENABLED="true"),
            dict(T100_LIVE_KILL_SWITCH="false"),
            dict(T100_GUARDIAN_ACCOUNT_KEY_SHA256="a"*64),
            dict(T100_GUARDIAN_POSITION_ID=""),
            dict(T100_GUARDIAN_DB_CA_FILE=""),
            dict(T100_GUARDIAN_READONLY_DB_DSN="http://bad"),
        ):
            with self.subTest(changes=sorted(changes)):
                with self.assertRaises(Exception):
                    validate_once_config(env(**changes))

    def test_not_acquiring_secrets_in_trading_mode(self):
        self.assertFalse(hasattr(GetOnlyMexcTransport, "_private_post"))


class SignedGetTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_hmac_covers_sorted_query_and_only_get(self):
        mock = HTTP()
        tr = GetOnlyMexcTransport(
            mock,api_key=KEY,api_secret="0123456789abcdefXyz",
        )
        result = await tr._private_get(
            "/api/v1/private/position/open_positions",
            {"symbol":"BTC_USDT","positionId":555},
        )
        self.assertEqual(result,[])
        self.assertEqual(len(mock.calls),1)
        path,kwargs=mock.calls[0]
        self.assertEqual(path,"/api/v1/private/position/open_positions")
        self.assertEqual(kwargs["params"],[("positionId",555),("symbol","BTC_USDT")])
        headers=kwargs["headers"]
        payload=KEY+headers["Request-Time"]+urlencode(kwargs["params"])
        sig=hmac.new(b"0123456789abcdefXyz",
                     payload.encode(),hashlib.sha256).hexdigest()
        self.assertEqual(headers["Signature"],sig)
        self.assertFalse(hasattr(tr,"_private_post"))

    async def test_endpoint_allowlist_blocks_mutation(self):
        mock=HTTP()
        tr=GetOnlyMexcTransport(
            mock,api_key=KEY,api_secret="0123456789abcdefXyz",
        )
        for path in (
            "/api/v1/private/order/create",
            "/api/v1/private/stoporder/cancel",
            "/api/v1/private/order/get/0",
            "https://evil.example/fake",
            "/api/v1/private/order/external/BTC_USDT/../../bad",
        ):
            with self.subTest(path=path):
                with self.assertRaises(GuardianOnceHalted):
                    await tr._private_get(path)
        self.assertEqual(mock.calls,[])

    async def test_602_api_error_is_redacted(self):
        mock=HTTP(MockResponse(dict(success=False,code=602,message="some URL with secret")))
        tr=GetOnlyMexcTransport(
            mock,api_key=KEY,api_secret="0123456789abcdefXyz",
        )
        with self.assertRaisesRegex(GuardianOnceHalted,"EXPLICITLY_REJECTED"):
            await tr._private_get("/api/v1/private/order/open_order_total_count")

    async def test_malformed_get_reply_fails_closed(self):
        mock=HTTP(MockResponse(dict(success=True,data=[])))
        tr=GetOnlyMexcTransport(
            mock,api_key=KEY,api_secret="0123456789abcdefXyz",
        )
        with self.assertRaises(GuardianOnceHalted):
            await tr._private_get("/api/v1/private/order/open_order_total_count")


class EndToEndNoTradingTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_proven_t100_checkpoint_observation_keeps_manual_separate(self):
        database=FakeDb()
        mexc=Venue()
        report=await inspect_once(
            conn=database,api_key=KEY,
            pinned_scope_sha256=SCOPE,pinned_position_id="555",
            mexc_probe=ReadOnlyMexcProbe(mexc),
        )
        self.assertEqual(report.status,"OWNED_NATIVE_MARKET_STOP_CURRENTLY_OBSERVED")
        self.assertEqual(report.foreign_position_count,1)
        self.assertTrue(report.managed_position_observed)
        self.assertTrue(report.native_market_stop_verified)
        self.assertFalse(report.entry_allowed)
        self.assertFalse(report.emergency_close_allowed)
        self.assertFalse(report.live_authorization)
        self.assertEqual(report.mexc_orders_sent,0)
        self.assertEqual(mexc.posts,[])
        serial=json.dumps(report.__dict__)
        self.assertNotIn(KEY,serial)
        self.assertNotIn("555",serial)

    async def test_absent_checkpoint_exits_without_any_exchange_request(self):
        database=FakeDb(cps=[])
        mexc=Venue()
        report=await inspect_once(
            conn=database,api_key=KEY,
            pinned_scope_sha256=SCOPE,pinned_position_id="555",
            mexc_probe=ReadOnlyMexcProbe(mexc),
        )
        self.assertEqual(report.status,"NO_MANAGED_CHECKPOINT_NO_EXCHANGE_ACTION")
        self.assertEqual(mexc.gets,[])
        self.assertEqual(mexc.posts,[])

    async def test_bad_role_halts_before_any_exchange_get(self):
        database=FakeDb(bad_priv="cp_update")
        mexc=Venue()
        with self.assertRaises(Exception):
            await inspect_once(
                conn=database,api_key=KEY,
                pinned_scope_sha256=SCOPE,pinned_position_id="555",
                mexc_probe=ReadOnlyMexcProbe(mexc),
            )
        self.assertEqual(mexc.gets,[])
        self.assertEqual(mexc.posts,[])


if __name__ == "__main__":
    unittest.main()

"""Offline tests for bounded MEXC private WebSocket observation."""
import gzip
import json
import unittest

from app.t100_live_private_ws import (
    PrivateWsHalted,
    _decode_message,
    observe_private_ws,
    ws_login_signature,
)


class FakeWs:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        import asyncio
        await asyncio.sleep(3600)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnect:
    def __init__(self, messages):
        self.ws = FakeWs(messages)
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.ws


class Clock:
    def __init__(self):
        self.value = 1_000_000_000

    def __call__(self):
        self.value += 1_000_000
        return self.value


class PrivateWsTests(unittest.IsolatedAsyncioTestCase):
    def test_login_signature_is_hmac_over_key_plus_time(self):
        import hashlib
        import hmac
        expected = hmac.new(
            b"secret", b"access12345", hashlib.sha256
        ).hexdigest()
        self.assertEqual(
            ws_login_signature("access", "secret", "12345"),
            expected,
        )

    def test_invalid_signature_inputs_rejected(self):
        for key, secret, ts in (
            ("", "secret", "1"),
            ("key", "", "1"),
            ("key", "secret", ""),
            ("key", "secret", "abc"),
        ):
            with self.assertRaises(ValueError):
                ws_login_signature(key, secret, ts)

    def test_gzip_json_decode(self):
        raw = gzip.compress(
            json.dumps({"channel": "pong", "data": 1}).encode()
        )
        self.assertEqual(_decode_message(raw)["channel"], "pong")

    def test_nonobject_message_rejected(self):
        with self.assertRaisesRegex(
            PrivateWsHalted, "OBJECT_REQUIRED"):
            _decode_message("[]")

    async def test_login_filter_and_redacted_events(self):
        messages = [
            json.dumps({
                "channel": "rs.login",
                "data": "success",
                "ts": "1",
            }),
            json.dumps({
                "channel": "push.personal.position",
                "data": {
                    "positionId": 123456,
                    "symbol": "SECRET_USDT",
                    "holdVol": 77,
                    "positionType": 2,
                    "state": 1,
                    "holdAvgPrice": 987.65,
                },
            }),
            json.dumps({
                "channel": "push.personal.stop.planorder",
                "data": {
                    "id": 999,
                    "positionId": 123456,
                    "symbol": "SECRET_USDT",
                    "state": 1,
                    "stopLossPrice": 1234.5,
                    "vol": 77,
                },
            }),
            json.dumps({
                "channel": "push.personal.order.deal",
                "data": {
                    "id": 555,
                    "orderId": 444,
                    "symbol": "SECRET_USDT",
                    "side": 3,
                    "vol": 77,
                    "price": 987.65,
                    "fee": -0.01,
                },
            }),
        ]
        connect = FakeConnect(messages)
        report = await observe_private_ws(
            api_key="ACCESS_SECRET_VALUE",
            api_secret="SECRET_SECRET_VALUE",
            duration_seconds=1,
            connect=connect,
            wall_time_ms=lambda: 1234567890000,
            monotonic_ns=Clock(),
        )
        self.assertTrue(report.login_verified)
        self.assertTrue(report.filter_sent)
        self.assertEqual(report.position_events_seen, 1)
        self.assertEqual(report.stop_plan_events_seen, 1)
        self.assertEqual(report.fill_events_seen, 1)
        self.assertEqual(report.private_events_seen, 3)
        self.assertFalse(report.entry_allowed)
        self.assertFalse(report.live_authorization)
        self.assertEqual(
            connect.ws.sent[0]["method"], "login")
        self.assertFalse(
            connect.ws.sent[0]["param"]["subscribe"])
        filters = connect.ws.sent[1]["param"]["filters"]
        self.assertEqual(
            {x["filter"] for x in filters},
            {"position", "stop.planorder", "order.deal"},
        )
        text = str(report)
        for secret in (
            "ACCESS_SECRET_VALUE", "SECRET_SECRET_VALUE",
            "SECRET_USDT", "123456", "987.65", "1234.5", "-0.01",
        ):
            self.assertNotIn(secret, text)
        self.assertIn(
            "positionId",
            report.channel_schema[
                "push.personal.position"]["field_types"],
        )

    async def test_login_rejection_fails_closed(self):
        connect = FakeConnect([
            json.dumps({"channel": "rs.error", "data": "bad auth"})
        ])
        with self.assertRaisesRegex(
            PrivateWsHalted, "WS_SERVER_ERROR"):
            await observe_private_ws(
                api_key="a", api_secret="b",
                duration_seconds=1, connect=connect,
            )

    async def test_private_event_before_login_fails(self):
        connect = FakeConnect([
            json.dumps({
                "channel": "push.personal.position",
                "data": {"positionId": 1},
            })
        ])
        with self.assertRaisesRegex(
            PrivateWsHalted, "BEFORE_AUTH_FILTER"):
            await observe_private_ws(
                api_key="a", api_secret="b",
                duration_seconds=1, connect=connect,
            )

    async def test_unexpected_private_channel_is_reported(self):
        connect = FakeConnect([
            json.dumps({"channel": "rs.login", "data": "success"}),
            json.dumps({
                "channel": "push.personal.asset",
                "data": {"currency": "USDT"},
            }),
        ])
        report = await observe_private_ws(
            api_key="a", api_secret="b",
            duration_seconds=1, connect=connect,
        )
        self.assertEqual(
            report.status,
            "COMPLETED_WITH_UNEXPECTED_PRIVATE_CHANNELS",
        )
        self.assertEqual(
            report.unexpected_private_channels,
            ("push.personal.asset",),
        )

    async def test_invalid_duration_rejected(self):
        for value in (0, 301, True, "30"):
            with self.assertRaises(ValueError):
                await observe_private_ws(
                    api_key="a", api_secret="b",
                    duration_seconds=value,
                )

    async def test_missing_credentials_rejected(self):
        with self.assertRaisesRegex(
            PrivateWsHalted, "CREDENTIALS_MISSING"):
            await observe_private_ws(
                api_key="", api_secret="",
                duration_seconds=1,
            )

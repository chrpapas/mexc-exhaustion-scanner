"""Offline access-only diagnostics; never contact MEXC or use real secrets."""
from __future__ import annotations

import asyncio
import unittest

from app.t100_live_private_positions_access import (
    OPEN_POSITIONS_PATH, _classify_response, _report,
    probe_private_positions_access,
)
from app.t100_live_private_positions_access_cli import run


class Response:
    def __init__(self, status_code=200, payload=None, *, broken_json=False):
        self.status_code = status_code
        self.payload = payload
        self.broken_json = broken_json

    def json(self):
        if self.broken_json:
            raise ValueError("SECRET_RAW_RESPONSE")
        return self.payload


class FakeTransport:
    def __init__(self, response=None, exception=None):
        self.calls = []
        self.response = response
        self.exception = exception

    async def get(self, path, *, headers):
        self.calls.append((path, headers))
        if self.exception is not None:
            raise self.exception
        return self.response


class PrivatePositionAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_signed_get_reports_only_read_success(self):
        transport = FakeTransport(Response(payload={
            "success": True, "code": 0,
            "data": [{"symbol": "SECRET_USDT", "positionId": 123456789,
                      "holdVol": "987654.321"}],
        }))
        header_calls = []

        def sign(ts, query):
            header_calls.append((ts, query))
            return {"ApiKey": "SECRET_API_KEY", "Signature": "SECRET_SIG"}

        got = await probe_private_positions_access(
            http_client=transport, signed_headers=sign,
            clock_ms=lambda: 1234567890000,
        )
        self.assertEqual(got["status"], "POSITIONS_READ_SUCCEEDED")
        self.assertEqual(got["http_status"], 200)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0][0], OPEN_POSITIONS_PATH)
        self.assertEqual(header_calls, [("1234567890000", "")])
        self.assertNotIn("SECRET", str(got))
        self.assertNotIn("holdVol", str(got))
        self.assertFalse(got["entry_allowed"])
        self.assertFalse(got["live_authorization"])
        self.assertFalse(got["account_wide_completeness_certified"])

    async def test_exchange_auth_and_scope_errors_have_fixed_codes_only(self):
        cases = {
            401: "AUTHENTICATION_REJECTED",
            402: "API_KEY_EXPIRED",
            406: "IP_NOT_WHITELISTED",
            510: "RATE_LIMITED",
            511: "ENDPOINT_PERMISSION_DENIED",
            513: "REQUEST_OR_TIMESTAMP_INVALID",
            602: "SIGNATURE_REJECTED",
            701: "ACCOUNT_READ_PERMISSION_REQUIRED",
            703: "TRADING_INFORMATION_READ_PERMISSION_REQUIRED",
            801: "EXCHANGE_MAINTENANCE",
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                transport = FakeTransport(Response(
                    status_code=200, payload={
                        "success": False, "code": code,
                        "message": "DO_NOT_LEAK_SECRET_OR_ACCOUNT_INFO"
                    },
                ))
                got = await probe_private_positions_access(
                    http_client=transport,
                    signed_headers=lambda ts, q: {"Signature": "not-printed"},
                    clock_ms=lambda: 1234,
                )
                self.assertEqual(got["status"], expected)
                self.assertEqual(len(transport.calls), 1)
                self.assertNotIn("DO_NOT_LEAK", str(got))
                self.assertFalse(got["live_authorization"])

    def test_http_classes_without_echoing_untrusted_response(self):
        cases = {
            401: "HTTP_AUTHENTICATION_REJECTED",
            403: "HTTP_AUTHENTICATION_REJECTED",
            429: "HTTP_RATE_LIMITED",
            500: "HTTP_SERVER_ERROR",
            503: "HTTP_SERVER_ERROR",
            404: "HTTP_CLIENT_ERROR",
        }
        for status, reason in cases.items():
            got = _classify_response(status, {"invalid": "SECRET"})
            self.assertEqual(got["status"], reason)
            self.assertNotIn("SECRET", str(got))
        self.assertIsNone(_classify_response(418, {})["http_status"])

    async def test_transport_timeout_is_redacted(self):
        got = await probe_private_positions_access(
            http_client=FakeTransport(exception=TimeoutError("SECRET_URL")),
            signed_headers=lambda ts, query: {"ApiKey": "secret"},
            clock_ms=lambda: 123,
        )
        self.assertEqual(got["status"], "NETWORK_TIMEOUT")
        self.assertNotIn("SECRET", str(got))

    async def test_transport_other_error_is_redacted(self):
        got = await probe_private_positions_access(
            http_client=FakeTransport(exception=RuntimeError("SECRET_URL")),
            signed_headers=lambda ts, query: {"ApiKey": "secret"},
            clock_ms=lambda: 123,
        )
        self.assertEqual(got["status"], "NETWORK_ERROR")
        self.assertNotIn("SECRET", str(got))

    async def test_invalid_signer_never_sends_any_request(self):
        transport = FakeTransport()
        got = await probe_private_positions_access(
            http_client=transport,
            signed_headers=lambda ts, query: (_ for _ in ()).throw(
                RuntimeError("DO_NOT_LEAK_SIGNATURE")
            ),
            clock_ms=lambda: 123,
        )
        self.assertEqual(got["status"], "AUTH_HEADERS_UNAVAILABLE")
        self.assertEqual(transport.calls, [])
        self.assertNotIn("DO_NOT_LEAK", str(got))

    async def test_unexpected_json_never_faked_as_success(self):
        for payload in (
            {"success": True, "code": 0, "data": None},
            {"success": True, "code": 0, "data": {}},
            {"success": True, "code": 0, "data": ["SECRET"]},
            {"success": True, "code": 602, "data": []},
        ):
            got = _classify_response(200, payload)
            self.assertEqual(got["status"], "MALFORMED_EXCHANGE_RESPONSE")
            self.assertFalse(got["entry_allowed"])
        got = _classify_response(200, {
            "success": False, "code": 123456789, "message": "SECRET"
        })
        self.assertEqual(got["status"], "UNCLASSIFIED_EXCHANGE_ERROR")
        self.assertNotIn("123456789", str(got))
        self.assertNotIn("SECRET", str(got))

    async def test_invalid_guard_early_return_before_network_import(self):
        checks = (
            ({}, "DIAGNOSTIC_PAPER_KILL_REQUIRED"),
            ({
                "T100_EXECUTION_MODE": "live",
                "T100_LIVE_ORDER_API_ENABLED": "false",
                "T100_LIVE_KILL_SWITCH": "true",
                "MEXC_API_KEY": "SECRET_KEY",
                "MEXC_API_SECRET": "SECRET_VALUE",
            }, "DIAGNOSTIC_PAPER_KILL_REQUIRED"),
            ({
                "T100_EXECUTION_MODE": "paper",
                "T100_LIVE_ORDER_API_ENABLED": "false",
                "T100_LIVE_KILL_SWITCH": "true",
            }, "DIAGNOSTIC_CREDENTIALS_MISSING"),
        )
        for env, expected in checks:
            got = await run(environ=env)
            self.assertEqual(got["status"], expected)
            self.assertFalse(got["mexc_orders_sent"])
            self.assertNotIn("SECRET", str(got))

    def test_result_cannot_echo_malicious_status_or_http_code(self):
        r = _report("SECRET_KEY=abc123", http_status=999)
        self.assertEqual(r["status"], "UNCLASSIFIED_EXCHANGE_ERROR")
        self.assertIsNone(r["http_status"])
        self.assertNotIn("SECRET", str(r))


if __name__ == "__main__":
    unittest.main()

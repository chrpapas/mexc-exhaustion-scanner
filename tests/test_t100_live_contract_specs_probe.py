"""Pure offline MEXC public contract-detail diagnostic tests."""
import unittest

from app.t100_live_contract_specs_probe import (
    PATH, probe_contract_specs, summarize_contract_response,
)


class FakeResponse:
    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc

    def raise_for_status(self):
        if self.exc:
            raise self.exc

    def json(self):
        return self.payload


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def get(self, path):
        self.calls.append(("GET", path))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class PublicContractProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_healthy_three_reads_do_not_use_private_endpoints(self):
        payload = {
            "success": True,
            "data": [
                {"symbol": "PRIVATE_SYMBOL_NEVER_SHARED", "contractSize": 1},
                {"symbol": "ANOTHER_PRIVATE_SYMBOL", "contractSize": 5},
            ],
        }
        client = FakeHttpClient([FakeResponse(payload)] * 3)
        state = {"now": 0.}
        def clock():
            state["now"] += .04
            return state["now"]
        async def no_sleep(_):
            return None
        result = await probe_contract_specs(
            client, clock=clock, sleep=no_sleep,
        )
        self.assertEqual(
            result["status"], "PUBLIC_CONTRACT_DETAIL_OBSERVATION_PASSED"
        )
        self.assertEqual(result["passed"], 3)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(client.calls, [("GET", PATH)] * 3)
        self.assertTrue(all(
            item["valid_contract_rows"] == 2
            for item in result["observations"]
        ))
        self.assertNotIn("PRIVATE_SYMBOL", str(result))
        self.assertFalse(result["live_authorization"])

    async def test_timeout_is_class_only_and_fail_closed(self):
        client = FakeHttpClient([
            TimeoutError("NO_SECRET_OR_URL_LEAK"),
            FakeResponse({"success": True, "data": []}),
            FakeResponse({"success": True, "data": [
                {"symbol": "REDACTED", "contractSize": 1}
            ]}),
        ])
        async def no_sleep(_):
            return None
        result = await probe_contract_specs(client, sleep=no_sleep)
        self.assertEqual(
            result["status"], "PUBLIC_CONTRACT_DETAIL_OBSERVATION_INCOMPLETE"
        )
        self.assertEqual(result["failed"], 2)
        self.assertEqual(
            result["observations"][0]["failure_type"], "TimeoutError"
        )
        self.assertEqual(
            result["observations"][1]["status"],
            "PUBLIC_CONTRACT_DATA_NOT_NONEMPTY_LIST",
        )
        self.assertNotIn("NO_SECRET_OR_URL_LEAK", str(result))
        self.assertNotIn("REDACTED", str(result))

    def test_schema_rejects_invalid_or_untrusted_payload(self):
        self.assertEqual(
            summarize_contract_response({"success": False})["status"],
            "PUBLIC_CONTRACT_SUCCESS_NOT_TRUE",
        )
        self.assertEqual(
            summarize_contract_response({"success": True, "data": {}})["status"],
            "PUBLIC_CONTRACT_DATA_NOT_NONEMPTY_LIST",
        )
        self.assertEqual(
            summarize_contract_response({"success": True, "data": [
                {"symbol": "", "contractSize": None}
            ]})["status"],
            "PUBLIC_CONTRACT_NO_VALID_CONTRACT_ROWS",
        )

    async def test_invalid_counts_fail_before_http(self):
        client = FakeHttpClient([])
        with self.assertRaises(ValueError):
            await probe_contract_specs(client, attempts=0)
        with self.assertRaises(ValueError):
            await probe_contract_specs(client, attempts=6)
        with self.assertRaises(ValueError):
            await probe_contract_specs(client, gap_seconds=-1)
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()

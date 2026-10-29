"""Offline tests: PnL synchronization is never live authorization."""
import copy
import unittest

from app.t100_live_pnl_sync_observation import observe_pnl_sync
from app.t100_live_pnl_sync_cli import run
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from tests.test_t100_live_cross_risk_observation import frame


def sample(a="0", b="0", u="0", margin="20"):
    p = copy.deepcopy(frame().positions)
    q = copy.deepcopy(p)
    assets = copy.deepcopy(frame().assets)
    p[0]["unRealizedPnl"] = a
    q[0]["unRealizedPnl"] = b
    assets[0]["unrealized"] = u
    assets[0]["positionMargin"] = margin
    return (p, assets, q)


class FakePrivate:
    def __init__(self, windows, fail=False):
        self.inputs = [copy.deepcopy(x) for group in windows for x in group]
        self.calls = []
        self.fail = fail

    async def _private_get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if self.fail:
            raise RuntimeError("SENSITIVE_RESPONSE")
        n = len(self.calls) - 1
        assert path == ("/api/v1/private/account/assets" if n % 3 == 1
                        else "/api/v1/private/position/open_positions")
        return copy.deepcopy(self.inputs[n])


class Tests(unittest.IsolatedAsyncioTestCase):
    async def check(self, windows):
        spy = FakePrivate(windows)
        times = iter(range(1000, 1000+2*len(windows)))
        result = await observe_pnl_sync(
            ReadOnlyMexcProbe(spy), monotonic_ms=lambda: next(times),
            samples=len(windows),
        )
        self.assertEqual(len(spy.calls), 3*len(windows))
        self.assertTrue(all(x[0] == "GET" for x in spy.calls))
        self.assertFalse(result.live_authorization)
        self.assertFalse(result.entry_allowed)
        self.assertFalse(result.venue_liquidation_certified)
        self.assertNotIn("TEST_USDT", str(result))
        return result

    async def test_all_stable_and_matched(self):
        r = await self.check([sample()]*3)
        self.assertEqual(r.status, "PNL_SYNC_STABLE_SAMPLES_RECONCILED_NONATOMIC")
        self.assertEqual(r.stable_unrealized_reconciled_windows, 3)

    async def test_all_moving(self):
        r = await self.check([
            sample("0","2","1"),
            sample("2","4","3"),
            sample("4","6","5"),
        ])
        self.assertEqual(r.status, "PNL_SYNC_MOVING_MARK_TO_MARKET_OBSERVED")
        self.assertEqual(r.position_pnl_moved_windows, 3)
        self.assertEqual(r.stable_unrealized_disagreement_windows, 0)

    async def test_stable_mismatch(self):
        r = await self.check([sample("0","0","5")]*3)
        self.assertEqual(r.status, "PNL_SYNC_STABLE_UNREALIZED_DISAGREEMENT")
        self.assertEqual(r.stable_unrealized_disagreement_windows, 3)

    async def test_margin_mismatch(self):
        r = await self.check([sample(margin="21")])
        self.assertEqual(r.status, "PNL_SYNC_POSITION_MARGIN_DISAGREEMENT")

    async def test_critical_drift(self):
        x = list(sample())
        x[2][0]["holdVol"] = "99"
        r = await self.check([x])
        self.assertEqual(r.status, "PNL_SYNC_INVENTORY_DRIFT_OBSERVED")

    async def test_missing_field_fails_without_partial_evidence(self):
        x = list(sample())
        x[1][0].pop("unrealized")
        r = await self.check([x])
        self.assertEqual(r.status, "PNL_SYNC_READ_OR_SCHEMA_UNVERIFIED")
        self.assertEqual(r.sampled_windows, 0)

    async def test_read_failure_stops_without_leaking(self):
        spy = FakePrivate([sample()], fail=True)
        r = await observe_pnl_sync(
            ReadOnlyMexcProbe(spy), monotonic_ms=lambda: 1,
            samples=1,
        )
        self.assertEqual(len(spy.calls), 1)
        self.assertEqual(r.status, "PNL_SYNC_READ_OR_SCHEMA_UNVERIFIED")
        self.assertNotIn("SENSITIVE_RESPONSE", str(r))

    async def test_four_samples_rejected_without_get(self):
        spy = FakePrivate([sample()])
        r = await observe_pnl_sync(
            ReadOnlyMexcProbe(spy), monotonic_ms=lambda: 1,
            samples=4,
        )
        self.assertEqual(r.status, "PNL_SYNC_INVALID_CONFIG")
        self.assertEqual(spy.calls, [])

    async def test_paper_guard(self):
        invalid = await run(environ={})
        self.assertEqual(invalid["status"], "PNL_SYNC_PAPER_KILL_REQUIRED")
        self.assertFalse(invalid["entry_allowed"])
        valid_paper = await run(environ={
            "T100_EXECUTION_MODE": "paper",
            "T100_LIVE_ORDER_API_ENABLED": "false",
            "T100_LIVE_KILL_SWITCH": "true",
        })
        self.assertEqual(valid_paper["status"], "PNL_SYNC_CREDENTIALS_MISSING")


if __name__ == "__main__":
    unittest.main()

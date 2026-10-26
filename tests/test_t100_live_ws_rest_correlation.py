"""Offline WS->REST lifecycle correlation tests."""
import unittest

from app.t100_live_ws_rest_correlation import PrivateWsRestCorrelator
from app.t100_live_ws_semantics import (
    FILL_CHANNEL, POSITION_CHANNEL, STOP_CHANNEL,
)


class Probe:
    def __init__(self):
        self.positions = [{
            "positionId": 10,
            "symbol": "SIREN_USDT",
            "holdVol": 20,
            "positionType": 2,
            "openType": 1,
            "state": 1,
        }]
        self.stops = [{
            "id": 99,
            "positionId": 10,
            "symbol": "SIREN_USDT",
            "state": 1,
            "isFinished": 0,
            "positionType": 2,
            "triggerSide": 0,
            "volType": 2,
            "profitLossVolType": "SAME",
            "stopLossPrice": 175,
            "vol": 20,
        }]
        self.fills = [{
            "id": 1,
            "orderId": 100,
            "symbol": "SIREN_USDT",
            "side": 3,
            "vol": 20,
            "price": 100,
        }]

    async def raw_open_positions(self, symbol=None):
        return [dict(x) for x in self.positions]

    async def raw_open_stops(self, symbol=None):
        return [dict(x) for x in self.stops]

    async def raw_order_deal_details(self, order_id):
        return [dict(x) for x in self.fills]

    async def raw_stop_page(self, page, size, *, is_finished=1):
        return []


def position_event(**kw):
    row = {
        "positionId": 10,
        "symbol": "SIREN_USDT",
        "holdVol": 20,
        "positionType": 2,
        "openType": 1,
        "state": 1,
    }
    row.update(kw)
    return row


def stop_event(**kw):
    row = {
        "id": 99,
        "positionId": 10,
        "symbol": "SIREN_USDT",
        "state": 1,
        "isFinished": 0,
        "positionType": 2,
        "triggerSide": 0,
        "volType": 2,
        "profitLossVolType": "SAME",
        "stopLossPrice": 175,
        "vol": 20,
    }
    row.update(kw)
    return row


def fill_event(**kw):
    row = {
        "id": 1,
        "orderId": 100,
        "symbol": "SIREN_USDT",
        "side": 3,
        "vol": 20,
        "price": 100,
        "feeCurrency": "USDT",
        "fee": 0.2,
        "timestamp": 2000,
        "category": 1,
        "positionMode": 1,
    }
    row.update(kw)
    return row


class WsRestCorrelationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.probe = Probe()
        self.c = PrivateWsRestCorrelator(self.probe)

    async def test_all_three_entry_lifecycle_channels_can_correlate(self):
        await self.c(POSITION_CHANNEL, position_event(), 2000)
        await self.c(STOP_CHANNEL, stop_event(), 2000)
        await self.c(FILL_CHANNEL, fill_event(), 2000)
        r = self.c.shareable()
        self.assertEqual(r["position_events_correlated"], 1)
        self.assertEqual(r["stop_events_correlated"], 1)
        self.assertEqual(r["fill_events_correlated"], 1)
        self.assertTrue(r["private_ws_full_lifecycle_verified"])
        self.assertFalse(r["live_authorization"])

    async def test_rest_value_drift_records_failure_without_raw_values(self):
        self.probe.fills[0]["vol"] = 19
        await self.c(FILL_CHANNEL, fill_event(), 2000)
        r = self.c.shareable()
        self.assertEqual(r["fill_events_correlated"], 0)
        self.assertEqual(r["fill_correlation_failures"], 1)
        self.assertFalse(r["private_ws_full_lifecycle_verified"])
        self.assertNotIn("SIREN_USDT", str(r))

    async def test_missing_rest_row_records_failure(self):
        self.probe.stops = []
        await self.c(STOP_CHANNEL, stop_event(), 2000)
        r = self.c.shareable()
        self.assertEqual(r["stop_correlation_failures"], 1)

    async def test_closed_position_event_is_not_falsely_correlated(self):
        await self.c(
            POSITION_CHANNEL,
            position_event(state=3, holdVol=0),
            2000,
        )
        r = self.c.shareable()
        self.assertEqual(r["position_correlation_failures"], 1)
        self.assertFalse(r["private_ws_full_lifecycle_verified"])


if __name__ == "__main__":
    unittest.main()

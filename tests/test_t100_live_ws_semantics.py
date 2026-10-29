"""Offline private WebSocket semantic/correlation tests."""
import unittest

from app.t100_live_ws_semantics import (
    FILL_CHANNEL,
    POSITION_CHANNEL,
    STOP_CHANNEL,
    assess_private_ws_payload,
    assess_private_ws_rest_correlation,
)


def position(**kw):
    row = {
        "positionId": 10,
        "symbol": "SIREN_USDT",
        "holdVol": 20,
        "positionType": 2,
        "openType": 1,
        "state": 1,
        "createTime": 1000,
        "updateTime": 2000,
        "leverage": 1,
    }
    row.update(kw)
    return row


def stop(**kw):
    row = {
        "id": 99,
        "symbol": "SIREN_USDT",
        "positionId": 10,
        "stopLossPrice": 175,
        "state": 1,
        "triggerSide": 0,
        "positionType": 2,
        "vol": 20,
        "stopLossVol": 20,
        "realityVol": 0,
        "placeOrderId": 0,
        "orderId": 0,
        "isFinished": 0,
        "profitLossVolType": "SAME",
        "volType": 2,
        "createTime": 1000,
        "updateTime": 2000,
    }
    row.update(kw)
    return row


def fill(**kw):
    row = {
        "id": 1,
        "symbol": "SIREN_USDT",
        "side": 3,
        "vol": 20,
        "price": 100,
        "feeCurrency": "USDT",
        "fee": 0.2,
        "timestamp": 2000,
        "profit": 0,
        "isTaker": True,
        "category": 1,
        "orderId": 100,
        "externalOid": "t100v1eabc",
        "positionMode": 1,
        "reduceOnly": False,
    }
    row.update(kw)
    return row


class WsSemanticTests(unittest.TestCase):
    def test_documented_position_payload_passes(self):
        r = assess_private_ws_payload(
            POSITION_CHANNEL, position(), envelope_ts=2001
        )
        self.assertTrue(r["payload_semantics_verified"])
        self.assertFalse(r["lifecycle_semantics_verified"])

    def test_documented_stop_payload_passes(self):
        r = assess_private_ws_payload(
            STOP_CHANNEL, stop(), envelope_ts=2001
        )
        self.assertTrue(r["payload_semantics_verified"])

    def test_entire_position_same_can_have_zero_explicit_volume(self):
        r = assess_private_ws_payload(
            STOP_CHANNEL,
            stop(vol=0, stopLossVol=0, profitLossVolType="SAME"),
        )
        self.assertTrue(r["payload_semantics_verified"])

    def test_separate_stop_requires_positive_stop_loss_volume(self):
        r = assess_private_ws_payload(
            STOP_CHANNEL,
            stop(
                vol=0,
                stopLossVol=0,
                profitLossVolType="SEPARATE",
            ),
        )
        self.assertFalse(r["payload_semantics_verified"])
        self.assertEqual(
            r["status"], "STOP_WS_SEPARATE_VOLUME_INVALID"
        )

    def test_documented_fill_payload_passes(self):
        r = assess_private_ws_payload(FILL_CHANNEL, fill())
        self.assertTrue(r["payload_semantics_verified"])

    def test_invalid_position_side_state_or_volume_blocks(self):
        for row in (
            position(positionType=9),
            position(state=9),
            position(holdVol=-1),
        ):
            with self.subTest(row=row):
                self.assertFalse(
                    assess_private_ws_payload(
                        POSITION_CHANNEL, row
                    )["payload_semantics_verified"]
                )

    def test_invalid_fill_identity_or_price_blocks(self):
        for row in (
            fill(id=0),
            fill(orderId=0),
            fill(vol=0),
            fill(price=0),
            fill(side=9),
        ):
            with self.subTest(row=row):
                self.assertFalse(
                    assess_private_ws_payload(
                        FILL_CHANNEL, row
                    )["payload_semantics_verified"]
                )

    def test_position_rest_correlation(self):
        data = position()
        r = assess_private_ws_rest_correlation(
            POSITION_CHANNEL,
            data,
            dict(data),
        )
        self.assertTrue(r["rest_correlation_verified"])
        self.assertTrue(r["lifecycle_semantics_verified"])
        self.assertFalse(r["live_authorization"])

    def test_stop_rest_correlation(self):
        data = stop()
        r = assess_private_ws_rest_correlation(
            STOP_CHANNEL,
            data,
            dict(data),
        )
        self.assertTrue(r["lifecycle_semantics_verified"])

    def test_fill_rest_correlation(self):
        data = fill()
        r = assess_private_ws_rest_correlation(
            FILL_CHANNEL,
            data,
            dict(data),
        )
        self.assertTrue(r["lifecycle_semantics_verified"])

    def test_rest_drift_blocks_lifecycle_semantics(self):
        data = fill()
        rest = dict(data, vol=19)
        r = assess_private_ws_rest_correlation(
            FILL_CHANNEL, data, rest
        )
        self.assertFalse(r["lifecycle_semantics_verified"])
        self.assertEqual(
            r["status"], "PRIVATE_WS_REST_CORRELATION_MISMATCH"
        )


if __name__ == "__main__":
    unittest.main()

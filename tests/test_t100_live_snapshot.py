"""Offline inventory validation tests: no exchange or credential access."""
import unittest
from decimal import Decimal
from app.t100_live_snapshot import validate_inventory_snapshot, SnapshotHalted


def position():
    return dict(symbol="SIREN_USDT", positionId="10", positionType=2, holdVol="5", state=1)


def stop():
    return dict(id="20", positionId="10", stopLossPrice="1.25", vol="5", state=1)


def snapshot(positions=None, stops=None, **kw):
    return validate_inventory_snapshot(
        positions=[] if positions is None else positions,
        stops=[] if stops is None else stops,
        observed_at_ms=kw.get("observed_at_ms", 1000),
        now_ms=kw.get("now_ms", 1500),
        positions_complete=kw.get("positions_complete", True),
        stops_complete=kw.get("stops_complete", True),
    )


class SnapshotTests(unittest.TestCase):
    def test_flat_complete(self):
        self.assertTrue(snapshot().flat)

    def test_nonflat(self):
        r = snapshot([position()], [stop()])
        self.assertFalse(r.flat)
        self.assertEqual(r.total_short_contracts, Decimal("5"))

    def test_entire_position_same_mode_allows_zero_explicit_volume(self):
        s = stop()
        s.update(vol="0", volType=2, profitLossVolType="SAME")
        r = snapshot([position()], [s])
        self.assertFalse(r.flat)

    def test_separate_mode_requires_positive_stop_loss_volume(self):
        s = stop()
        s.update(
            vol="0",
            volType=2,
            profitLossVolType="SEPARATE",
            stopLossVol="0",
        )
        with self.assertRaisesRegex(SnapshotHalted, "INVALID_STOP_VOLUME"):
            snapshot([position()], [s])

    def test_partial_position_pages_block(self):
        with self.assertRaisesRegex(SnapshotHalted, "PAGINATION"):
            snapshot(positions_complete=False)

    def test_partial_stop_pages_block(self):
        with self.assertRaisesRegex(SnapshotHalted, "PAGINATION"):
            snapshot(stops_complete=False)

    def test_stale_blocks(self):
        with self.assertRaisesRegex(SnapshotHalted, "STALE"):
            snapshot(now_ms=5000)

    def test_future_timestamp_blocks(self):
        with self.assertRaisesRegex(SnapshotHalted, "STALE"):
            snapshot(now_ms=999)

    def test_duplicate_positions_block(self):
        with self.assertRaisesRegex(SnapshotHalted, "DUPLICATE_POSITION"):
            snapshot([position(), position()])

    def test_duplicate_stops_block(self):
        with self.assertRaisesRegex(SnapshotHalted, "DUPLICATE_STOP"):
            snapshot(stops=[stop(), stop()])

    def test_unknown_side_blocks(self):
        p = position()
        p["positionType"] = 9
        with self.assertRaisesRegex(SnapshotHalted, "UNKNOWN_POSITION_SIDE"):
            snapshot([p])

    def test_system_held_position_blocks(self):
        p = position()
        p["state"] = 2
        with self.assertRaisesRegex(SnapshotHalted, "POSITION_NOT_ORDINARY_HOLDING"):
            snapshot([p])

    def test_closed_position_blocks(self):
        p = position()
        p["state"] = 3
        with self.assertRaisesRegex(SnapshotHalted, "POSITION_NOT_ORDINARY_HOLDING"):
            snapshot([p])

    def test_missing_position_state_blocks(self):
        p = position()
        del p["state"]
        with self.assertRaisesRegex(SnapshotHalted, "INVALID_POSITION_STATE"):
            snapshot([p])

    def test_missing_quantity_blocks(self):
        p = position()
        del p["holdVol"]
        with self.assertRaisesRegex(SnapshotHalted, "INVALID_POSITION_VOLUME"):
            snapshot([p])

    def test_inactive_stop_blocks(self):
        s = stop()
        s["state"] = 2
        with self.assertRaisesRegex(SnapshotHalted, "INACTIVE_STOP"):
            snapshot(stops=[s])

    def test_nan_stop_blocks(self):
        s = stop()
        s["stopLossPrice"] = "NaN"
        with self.assertRaisesRegex(SnapshotHalted, "INVALID_STOP_PRICE"):
            snapshot(stops=[s])

    def test_unmatched_stop_is_not_flat(self):
        self.assertFalse(snapshot(stops=[stop()]).flat)

    def test_long_position_is_not_flat(self):
        p = position()
        p["positionType"] = 1
        r = snapshot([p])
        self.assertFalse(r.flat)
        self.assertEqual(r.total_short_contracts, Decimal(0))

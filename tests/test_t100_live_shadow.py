"""Pure single-snapshot shadow checks; no network and no order writes."""
import unittest
from decimal import Decimal as D
from app.t100_live_shadow import shadow_reconcile, ShadowHalted
from app.t100_live_snapshot import SnapshotHalted
from app.t100_live_supervisor import ManagedShort


def position():
    return dict(symbol="SIREN_USDT", positionId="10", positionType=2, holdVol="20", state=1)


def stop():
    return dict(positionId="10", id="99", state=1, stopLossPrice="175", vol="20")


def wanted():
    return ManagedShort("SIREN_USDT", "10", D("20"), D("100"), "99",
                        D("175"), D("0"), False)


def run(positions=None, stops=None, expected=None, **kw):
    return shadow_reconcile(
        positions=[position()] if positions is None else positions,
        stops=[stop()] if stops is None else stops,
        expected=[wanted()] if expected is None else expected,
        observed_at_ms=kw.get("observed_at_ms", 1000),
        now_ms=kw.get("now_ms", 1500),
        positions_complete=kw.get("positions_complete", True),
        stops_complete=kw.get("stops_complete", True),
        observed_lows=kw.get("observed_lows"),
        price_ticks=kw.get("price_ticks"),
    )


class ShadowTests(unittest.TestCase):
    def test_healthy_no_orders(self):
        r = run()
        self.assertEqual(r.mode, "SHADOW_ONLY")
        self.assertEqual(r.proposals, ())

    def test_flat_account(self):
        r = run([], [], [])
        self.assertEqual(r.position_count, 0)

    def test_trailing_proposal_is_data_only(self):
        r = run(observed_lows={"SIREN_USDT": D("86")},
                price_ticks={"SIREN_USDT": D("0.01")})
        self.assertEqual(r.proposals, (("SIREN_USDT", "99", D("86.50")),))

    def test_stale_snapshot_fails(self):
        with self.assertRaisesRegex(SnapshotHalted, "STALE"):
            run(now_ms=9000)

    def test_incomplete_positions_fail(self):
        with self.assertRaisesRegex(SnapshotHalted, "PAGINATION"):
            run(positions_complete=False)

    def test_incomplete_stops_fail(self):
        with self.assertRaisesRegex(SnapshotHalted, "PAGINATION"):
            run(stops_complete=False)

    def test_unknown_position_fails(self):
        p = position()
        p["positionId"] = "11"
        with self.assertRaisesRegex(ShadowHalted, "UNKNOWN_POSITION"):
            run(positions=[p])

    def test_unknown_stop_fails(self):
        s = stop()
        s["id"] = "100"
        with self.assertRaisesRegex(ShadowHalted, "UNKNOWN_POSITION_OR_STOP"):
            run(stops=[s])

    def test_entire_position_same_stop_covers_without_explicit_volume(self):
        s = stop()
        s.update(
            vol="0",
            volType=2,
            profitLossVolType="SAME",
        )
        r = run(stops=[s])
        self.assertEqual(r.protected_positions, 1)

    def test_separate_mode_requires_explicit_stop_loss_volume(self):
        s = stop()
        s.update(
            vol="0",
            volType=2,
            profitLossVolType="SEPARATE",
            stopLossVol="0",
        )
        with self.assertRaisesRegex(
            SnapshotHalted, "INVALID_STOP_VOLUME"
        ):
            run(stops=[s])

    def test_undercovered_stop_fails(self):
        s = stop()
        s["vol"] = "19"
        with self.assertRaisesRegex(ShadowHalted, "INSUFFICIENT_NATIVE_STOP"):
            run(stops=[s])

    def test_stop_loosened_fails(self):
        s = stop()
        s["stopLossPrice"] = "176"
        with self.assertRaisesRegex(ShadowHalted, "NATIVE_STOP_LOOSENED"):
            run(stops=[s])

    def test_position_quantity_drift_fails(self):
        p = position()
        p["holdVol"] = "19"
        with self.assertRaisesRegex(ShadowHalted, "POSITION_SIDE_OR_VOLUME_DRIFT"):
            run(positions=[p])

    def test_long_position_fails(self):
        p = position()
        p["positionType"] = 1
        with self.assertRaisesRegex(ShadowHalted, "POSITION_SIDE_OR_VOLUME_DRIFT"):
            run(positions=[p])

    def test_extra_position_fails(self):
        p = dict(position(), positionId="11")
        with self.assertRaisesRegex(ShadowHalted, "EXPECTED_INVENTORY_COUNT_MISMATCH"):
            run(positions=[position(), p])

    def test_extra_stop_fails(self):
        s = dict(stop(), id="100")
        with self.assertRaisesRegex(ShadowHalted, "EXPECTED_INVENTORY_COUNT_MISMATCH"):
            run(stops=[stop(), s])

    def test_duplicate_symbol_fails(self):
        extra = ManagedShort("SIREN_USDT", "11", D("1"), D("100"), "100",
                             D("175"), D("0"), False)
        with self.assertRaisesRegex(ShadowHalted, "DUPLICATE_EXPECTED_POSITION"):
            run(expected=[wanted(), extra])

    def test_missing_tick_fails(self):
        with self.assertRaisesRegex(ShadowHalted, "INCOMPLETE_MARKET_DATA_PAIR"):
            run(observed_lows={"SIREN_USDT": D("86")})

    def test_market_data_for_unmanaged_symbol_fails(self):
        with self.assertRaisesRegex(ShadowHalted, "UNRECOGNIZED_MARKET_DATA_SYMBOL"):
            run(observed_lows={"OTHER_USDT": D("86")},
                price_ticks={"OTHER_USDT": D("0.01")})

    def test_invalid_expected_price_fails(self):
        w = ManagedShort("SIREN_USDT", "10", D("20"), D("NaN"), "99",
                         D("175"), D("0"), False)
        with self.assertRaisesRegex(ShadowHalted, "INVALID_EXPECTED_QUANTITY"):
            run(expected=[w])

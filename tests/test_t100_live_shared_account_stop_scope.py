"""Offline T100-only protected-stop scope in shared MEXC account."""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from decimal import Decimal as D

from app.t100_live_durable_recovery import (
    DurableRecoveryReport, PositionCheckpoint,
)
from app.t100_live_shared_account_stop_scope import (
    observe_t100_stop_scope_in_shared_account,
)
from app.t100_live_supervisor import ManagedShort


def fixture():
    owned = {
        "symbol": "SIREN_USDT", "positionId": "10",
        "holdVol": "20", "positionType": 2, "openType": 2,
    }
    manual_cross = {
        "symbol": "MANUALA_USDT", "positionId": "11",
        "holdVol": "5", "positionType": 2, "openType": 2,
    }
    manual_isolated = {
        "symbol": "MANUALB_USDT", "positionId": "12",
        "holdVol": "7", "positionType": 1, "openType": 1,
    }
    stop = {
        "positionId": "10", "id": "99", "state": 1,
        "stopLossPrice": "175", "vol": "20",
    }
    cp = PositionCheckpoint(
        symbol="SIREN_USDT", external_oid="t100-test",
        exchange_order_id="456", position_id="10", contracts=D("20"),
        entry_price=D("100"), stop_order_id="99", stop_price=D("175"),
        best_profit_pct=D("0"), armed=False, lifecycle_state="OPEN",
        checkpoint_version=1,
    )
    recovered = DurableRecoveryReport(
        recovered=(ManagedShort(
            "SIREN_USDT", "10", D("20"), D("100"), "99",
            D("175"), D("0"), False,
        ),),
        checkpoint_count=1, exchange_position_count=1,
        exchange_stop_count=1,
    )
    return [cp], recovered, [owned, manual_cross, manual_isolated], [stop]


class SharedStopScopeTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, **changes):
        cp, report, positions, stops = fixture()
        args = dict(
            checkpoints=cp,
            recovered=report,
            account_positions=positions,
            account_stops=stops,
        )
        args.update(changes)
        return await observe_t100_stop_scope_in_shared_account(**args)

    async def test_two_stopless_manual_positions_without_t100(self):
        cp, report, positions, stops = fixture()
        result = await self.call(
            checkpoints=[],
            recovered=None,
            account_positions=positions[1:],
            account_stops=[],
        )
        self.assertEqual(
            result.status, "SHARED_STOP_SCOPE_MANUAL_ONLY_NONAUTHORIZING"
        )
        self.assertEqual(result.account_positions, 2)
        self.assertEqual(result.manual_positions, 2)
        self.assertEqual(result.managed_positions, 0)
        self.assertEqual(result.manual_positions_without_positive_sl, 2)
        self.assertFalse(result.entry_allowed)
        self.assertFalse(result.live_authorization)
        self.assertFalse(result.mexc_orders_sent)

    async def test_t100_owned_native_stop_verified_manual_positions_untouched(self):
        result = await self.call()
        self.assertEqual(
            result.status, "SHARED_STOP_SCOPE_T100_STOPS_OBSERVED_NONAUTHORIZING",
        )
        self.assertEqual(result.account_positions, 3)
        self.assertEqual(result.managed_positions, 1)
        self.assertEqual(result.manual_positions, 2)
        self.assertEqual(result.manual_positions_without_positive_sl, 2)
        self.assertTrue(result.t100_native_stops_observed_non_authorizing)
        self.assertFalse(result.owner_provenance_independently_certified)
        self.assertFalse(result.account_collateral_certified)
        self.assertFalse(result.live_adapter_certified)
        self.assertFalse(result.entry_allowed)
        self.assertFalse(result.live_authorization)
        self.assertFalse(result.mexc_orders_sent)

    async def test_stop_price_or_volume_drift_blocks(self):
        cp, recovered, positions, stops = fixture()
        for changed in (
            {"stopLossPrice": "176"},
            {"vol": "19"},
            {"state": 0},
        ):
            with self.subTest(changed=changed):
                altered = [dict(stops[0], **changed)]
                out = await self.call(account_stops=altered)
                self.assertEqual(
                    out.status,
                    "SHARED_STOP_SCOPE_T100_NATIVE_STOP_UNVERIFIED",
                )
                self.assertFalse(out.t100_native_stops_observed_non_authorizing)
                self.assertFalse(out.entry_allowed)

    async def test_lost_managed_stop_and_orphan_manual_stop_block(self):
        cp, recovered, positions, stops = fixture()
        missing = await self.call(account_stops=[])
        self.assertEqual(
            missing.status, "SHARED_STOP_SCOPE_PARTITION_UNVERIFIED"
        )
        orphan = await self.call(account_stops=[
            *stops, {"positionId": "4321", "id": "101",
                     "stopLossPrice": "500"},
        ])
        self.assertEqual(
            orphan.status, "SHARED_STOP_SCOPE_PARTITION_UNVERIFIED"
        )

    async def test_manual_tp_only_row_does_not_require_manual_stop(self):
        cp, recovered, positions, stops = fixture()
        tp = {"positionId": "12", "id": "112",
              "stopLossPrice": "0", "takeProfitPrice": "123"}
        result = await self.call(account_stops=[*stops, tp])
        self.assertEqual(
            result.status, "SHARED_STOP_SCOPE_T100_STOPS_OBSERVED_NONAUTHORIZING"
        )
        self.assertEqual(result.manual_positions_without_positive_sl, 2)

    async def test_manual_same_symbol_as_owned_and_more_than_four_fail(self):
        cp, recovered, positions, stops = fixture()
        conflict = dict(positions[1], symbol="SIREN_USDT")
        r = await self.call(
            account_positions=[positions[0], conflict, positions[2]]
        )
        self.assertEqual(
            r.status, "SHARED_STOP_SCOPE_PARTITION_UNVERIFIED"
        )
        extra = dict(positions[1], symbol="EXTRA1_USDT", positionId="13")
        extra2 = dict(positions[1], symbol="EXTRA2_USDT", positionId="14")
        result = await self.call(account_positions=[
            *positions, extra, extra2,
        ])
        self.assertEqual(
            result.status, "SHARED_STOP_SCOPE_PARTITION_UNVERIFIED"
        )

    async def test_forged_or_mismatched_recovery_not_accepted(self):
        cp, recovered, positions, stops = fixture()
        result = await self.call(recovered=None)
        self.assertEqual(
            result.status, "SHARED_STOP_SCOPE_DURABLE_PROVENANCE_UNVERIFIED"
        )
        wrong = replace(recovered, recovered=(
            replace(recovered.recovered[0], position_id="DIFFERENT"),
        ))
        result = await self.call(recovered=wrong)
        self.assertEqual(
            result.status, "SHARED_STOP_SCOPE_DURABLE_IDENTITY_DRIFT"
        )
        self.assertFalse(result.entry_allowed)

    async def test_manual_rows_never_mutated(self):
        cp, recovered, positions, stops = fixture()
        before = copy.deepcopy((positions, stops))
        await self.call(account_positions=positions, account_stops=stops)
        self.assertEqual((positions, stops), before)


if __name__ == "__main__":
    unittest.main()

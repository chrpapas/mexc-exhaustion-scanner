"""Pure deterministic non-flat restart reconstruction tests."""
import unittest
from decimal import Decimal as D

from app.t100_live_durable_recovery import (
    DurableRecoveryHalted,
    checkpoint_from_row,
    recover_nonflat,
)


def checkpoint(**extra):
    row = {
        "symbol": "SIREN_USDT",
        "external_oid": "t100v1eabc",
        "exchange_order_id": "100",
        "position_id": "10",
        "contracts": D("20"),
        "entry_price": D("100"),
        "stop_order_id": "99",
        "stop_price": D("175"),
        "best_profit_pct": D("0"),
        "armed": False,
        "lifecycle_state": "OPEN",
        "checkpoint_version": 1,
    }
    row.update(extra)
    return row


def intent(**extra):
    row = {
        "symbol": "SIREN_USDT",
        "external_oid": "t100v1eabc",
        "exchange_order_id": "100",
        "volume_contracts": D("20"),
        "stop_price": D("175"),
        "state": "ACK",
    }
    row.update(extra)
    return row


def position(**extra):
    row = {
        "symbol": "SIREN_USDT",
        "positionId": "10",
        "positionType": 2,
        "state": 1,
        "holdVol": D("20"),
        "holdAvgPrice": D("100"),
    }
    row.update(extra)
    return row


def stop(**extra):
    row = {
        "symbol": "SIREN_USDT",
        "positionId": "10",
        "id": "99",
        "state": 1,
        "stopLossPrice": D("175"),
        "vol": D("20"),
    }
    row.update(extra)
    return row


def run(**kw):
    return recover_nonflat(
        checkpoint_rows=kw.get("checkpoints", [checkpoint()]),
        intent_rows=kw.get("intents", [intent()]),
        stop_amendment_rows=kw.get("amendments", []),
        positions=kw.get("positions", [position()]),
        stops=kw.get("stops", [stop()]),
        positions_complete=kw.get("positions_complete", True),
        stops_complete=kw.get("stops_complete", True),
    )


class DurableRecoveryTests(unittest.TestCase):
    def test_exact_nonflat_reconstruction(self):
        r = run()
        self.assertEqual(r.mode, "SHADOW_ONLY_RECOVERED_NONFLAT")
        self.assertFalse(r.live_authorization)
        self.assertEqual(len(r.recovered), 1)
        self.assertEqual(r.recovered[0].position_id, "10")
        self.assertEqual(r.recovered[0].stop_order_id, "99")
        self.assertEqual(r.recovered[0].contracts, D("20"))

    def test_armed_trailing_state_reconstructs(self):
        r = run(
            checkpoints=[checkpoint(
                stop_price=D("86.5"), best_profit_pct=D("14"),
                armed=True, checkpoint_version=7)],
            stops=[stop(stopLossPrice=D("86.5"))],
        )
        self.assertTrue(r.recovered[0].armed)
        self.assertEqual(r.recovered[0].best_profit_pct, D("14"))
        self.assertEqual(r.recovered[0].stop_price, D("86.5"))

    def test_unverified_inventory_completeness_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "COMPLETENESS_UNVERIFIED"):
            run(positions_complete=False)
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "COMPLETENESS_UNVERIFIED"):
            run(stops_complete=False)

    def test_missing_checkpoint_blocks_nonflat(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "CHECKPOINT_COUNT_INVALID"):
            run(checkpoints=[])

    def test_too_many_checkpoints_blocks(self):
        rows = [
            checkpoint(
                symbol=f"S{i}_USDT",
                external_oid=f"oid{i}",
                exchange_order_id=str(100+i),
                position_id=str(10+i),
                stop_order_id=str(90+i),
            )
            for i in range(5)
        ]
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "CHECKPOINT_COUNT_INVALID"):
            run(checkpoints=rows)

    def test_checkpoint_requires_open_lifecycle(self):
        for state in ("REVIEW", "CLOSING", "CLOSED"):
            with self.assertRaisesRegex(
                DurableRecoveryHalted, "CHECKPOINT_NOT_OPEN"):
                checkpoint_from_row(checkpoint(lifecycle_state=state))

    def test_checkpoint_trailing_state_must_be_coherent(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "TRAILING_STATE_INCOHERENT"):
            checkpoint_from_row(
                checkpoint(best_profit_pct=D("14"), armed=False))
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "TRAILING_STATE_INCOHERENT"):
            checkpoint_from_row(
                checkpoint(best_profit_pct=D("13.9"), armed=True))

    def test_checkpoint_version_must_be_positive_integer(self):
        for value in (0, -1, True, "1.5"):
            with self.assertRaisesRegex(
                DurableRecoveryHalted, "CHECKPOINT_VERSION"):
                checkpoint_from_row(checkpoint(checkpoint_version=value))

    def test_unresolved_order_intent_blocks(self):
        for state in ("PREPARED", "SUBMITTING", "UNKNOWN", "REVIEW"):
            with self.assertRaisesRegex(
                DurableRecoveryHalted, "UNRESOLVED_ORDER_INTENT"):
                run(intents=[intent(state=state)])

    def test_checkpoint_intent_must_be_ack(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "CHECKPOINT_INTENT_NOT_ACK"):
            run(intents=[intent(state="CLOSED")])

    def test_checkpoint_must_have_matching_intent(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "CHECKPOINT_INTENT_MISSING"):
            run(intents=[])

    def test_ack_intent_without_checkpoint_blocks(self):
        extra = intent(
            symbol="OTHER_USDT",
            external_oid="other",
            exchange_order_id="200",
        )
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "ACK_INTENT_WITHOUT_OPEN_CHECKPOINT"):
            run(intents=[intent(), extra])

    def test_order_id_drift_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "ORDER_ID_DRIFT"):
            run(intents=[intent(exchange_order_id="999")])

    def test_intent_symbol_drift_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "SYMBOL_DRIFT"):
            run(intents=[intent(symbol="OTHER_USDT")])

    def test_intent_volume_drift_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "INTENT_VOLUME_DRIFT"):
            run(intents=[intent(volume_contracts=D("19"))])

    def test_checkpoint_stop_cannot_be_looser_than_entry_stop(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "STOP_LOOSER_THAN_ENTRY_INTENT"):
            run(
                checkpoints=[checkpoint(stop_price=D("176"))],
                stops=[stop(stopLossPrice=D("176"))],
            )

    def test_unresolved_stop_amendment_blocks(self):
        for state in ("PREPARED", "SUBMITTING", "UNKNOWN"):
            with self.assertRaisesRegex(
                DurableRecoveryHalted, "UNRESOLVED_STOP_AMENDMENT"):
                run(amendments=[{"state": state}])

    def test_verified_stop_amendment_does_not_block(self):
        r = run(amendments=[{"state": "VERIFIED"}])
        self.assertEqual(len(r.recovered), 1)

    def test_position_count_mismatch_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "POSITION_COUNT_MISMATCH"):
            run(positions=[])

    def test_stop_count_mismatch_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "STOP_COUNT_MISMATCH"):
            run(stops=[])

    def test_position_identity_drift_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "POSITION_NOT_FOUND"):
            run(positions=[position(positionId="11")])

    def test_position_side_drift_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "POSITION_SIDE_DRIFT"):
            run(positions=[position(positionType=1)])

    def test_position_state_drift_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "POSITION_STATE_DRIFT"):
            run(positions=[position(state=2)])

    def test_position_volume_drift_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "POSITION_VOLUME_DRIFT"):
            run(positions=[position(holdVol=D("19"))])

    def test_entry_price_drift_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "ENTRY_PRICE_DRIFT"):
            run(positions=[position(holdAvgPrice=D("101"))])

    def test_tiny_entry_price_rounding_is_tolerated(self):
        r = run(positions=[position(holdAvgPrice=D("100.0000005"))])
        self.assertEqual(len(r.recovered), 1)

    def test_stop_identity_drift_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "STOP_NOT_FOUND"):
            run(stops=[stop(id="100")])

    def test_inactive_stop_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "STOP_NOT_ACTIVE"):
            run(stops=[stop(state=2)])

    def test_stop_symbol_drift_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "STOP_SYMBOL_DRIFT"):
            run(stops=[stop(symbol="OTHER_USDT")])

    def test_stop_price_drift_blocks_even_if_tighter(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "STOP_PRICE_DRIFT"):
            run(stops=[stop(stopLossPrice=D("174"))])

    def test_stop_volume_must_match_exactly(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "STOP_VOLUME_DRIFT"):
            run(stops=[stop(vol=D("21"))])
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "STOP_VOLUME_DRIFT"):
            run(stops=[stop(vol=D("19"))])

    def test_duplicate_checkpoint_symbol_blocks(self):
        c2 = checkpoint(
            external_oid="oid2", exchange_order_id="200",
            position_id="11", stop_order_id="100",
        )
        i2 = intent(
            external_oid="oid2", exchange_order_id="200",
        )
        p2 = position(positionId="11")
        s2 = stop(positionId="11", id="100")
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "DUPLICATE_CHECKPOINT_SYMBOL"):
            run(
                checkpoints=[checkpoint(), c2],
                intents=[intent(), i2],
                positions=[position(), p2],
                stops=[stop(), s2],
            )

    def test_extra_exchange_inventory_cannot_be_ignored(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "POSITION_COUNT_MISMATCH"):
            run(positions=[position(), position(
                symbol="OTHER_USDT", positionId="11")])
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "STOP_COUNT_MISMATCH"):
            run(stops=[stop(), stop(positionId="11", id="100")])

    def test_malformed_checkpoint_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "MALFORMED_CHECKPOINT"):
            run(checkpoints=[None])

    def test_malformed_exchange_rows_block(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "RECOVERY_INPUT_RESPONSE_AMBIGUOUS"):
            run(positions=[None])
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "RECOVERY_INPUT_RESPONSE_AMBIGUOUS"):
            run(stops=[None])

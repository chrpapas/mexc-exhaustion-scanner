"""Pure deterministic non-flat restart reconstruction tests."""
import unittest
from decimal import Decimal as D

from app.t100_live_durable_recovery import (
    DurableRecoveryHalted,
    DurableRestartRecoveryGate,
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



def revisions_for(row):
    n = int(row["checkpoint_version"])
    final_stop = D(str(row["stop_price"]))
    final_best = D(str(row["best_profit_pct"]))
    final_armed = bool(row["armed"])
    rows = []
    for version in range(1, n + 1):
        if version == 1:
            stop_price = final_stop + D(n - 1)
            best = D("0")
            armed = False
            reason = "ENTRY_PROTECTED"
        else:
            stop_price = final_stop + D(n - version)
            if final_armed:
                best = D("14") if final_best == D("14") else max(
                    D("14"),
                    final_best - D(n - version) * D("0.1"),
                )
                armed = True
            else:
                best = final_best
                armed = False
            reason = "STOP_VERIFIED"
        rows.append({
            "position_id": row["position_id"],
            "checkpoint_version": version,
            "write_reason": reason,
            "symbol": row["symbol"],
            "external_oid": row["external_oid"],
            "exchange_order_id": row["exchange_order_id"],
            "contracts": row["contracts"],
            "entry_price": row["entry_price"],
            "stop_order_id": row["stop_order_id"],
            "stop_price": stop_price,
            "best_profit_pct": best,
            "armed": armed,
            "lifecycle_state": "OPEN",
        })
    return rows


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


def order(**extra):
    row = {
        "externalOid": "t100v1eabc",
        "orderId": "100",
        "symbol": "SIREN_USDT",
        "side": 3,
        "state": 3,
        "positionId": "10",
        "dealVol": D("20"),
        "dealAvgPrice": D("100"),
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
    checkpoints = kw.get("checkpoints", [checkpoint()])
    if "revisions" in kw:
        revisions = kw["revisions"]
    elif all(isinstance(row, dict) for row in checkpoints):
        revisions = [
            rev for row in checkpoints for rev in revisions_for(row)
        ]
    else:
        revisions = []
    return recover_nonflat(
        checkpoint_rows=checkpoints,
        revision_rows=revisions,
        intent_rows=kw.get("intents", [intent()]),
        stop_amendment_rows=kw.get("amendments", []),
        orders=kw.get("orders", [order()]),
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

    def test_four_slot_nonflat_reconstruction(self):
        checkpoints = []
        intents = []
        orders = []
        positions = []
        stops = []
        for i in range(4):
            symbol = f"S{i}_USDT"
            oid = f"oid{i}"
            order_id = str(100 + i)
            position_id = str(10 + i)
            stop_id = str(90 + i)
            checkpoints.append(checkpoint(
                symbol=symbol,
                external_oid=oid,
                exchange_order_id=order_id,
                position_id=position_id,
                stop_order_id=stop_id,
            ))
            intents.append(intent(
                symbol=symbol,
                external_oid=oid,
                exchange_order_id=order_id,
            ))
            orders.append(order(
                externalOid=oid,
                orderId=order_id,
                symbol=symbol,
                positionId=position_id,
            ))
            positions.append(position(
                symbol=symbol,
                positionId=position_id,
            ))
            stops.append(stop(
                symbol=symbol,
                positionId=position_id,
                id=stop_id,
            ))
        r = run(
            checkpoints=checkpoints,
            intents=intents,
            orders=orders,
            positions=positions,
            stops=stops,
        )
        self.assertEqual(len(r.recovered), 4)
        self.assertEqual(r.checkpoint_count, 4)
        self.assertEqual(
            {x.symbol for x in r.recovered},
            {"S0_USDT", "S1_USDT", "S2_USDT", "S3_USDT"},
        )

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

    def test_missing_revision_history_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "REVISION_POSITION_SET_MISMATCH"):
            run(revisions=[])

    def test_revision_count_mismatch_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "REVISION_COUNT_MISMATCH"):
            run(
                checkpoints=[checkpoint(
                    stop_price=D("90"), best_profit_pct=D("14"),
                    armed=True, checkpoint_version=2)],
                revisions=[revisions_for(checkpoint())[0]],
                stops=[stop(stopLossPrice=D("90"))],
            )

    def test_duplicate_revision_version_blocks(self):
        base = checkpoint(
            stop_price=D("90"), best_profit_pct=D("14"),
            armed=True, checkpoint_version=2)
        rows = revisions_for(base)
        rows[1]["checkpoint_version"] = 1
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "DUPLICATE_CHECKPOINT_REVISION_VERSION"):
            run(
                checkpoints=[base],
                revisions=rows,
                stops=[stop(stopLossPrice=D("90"))],
            )

    def test_revision_sequence_gap_blocks(self):
        base = checkpoint(
            stop_price=D("89"), best_profit_pct=D("14"),
            armed=True, checkpoint_version=3)
        rows = revisions_for(base)
        rows[1]["checkpoint_version"] = 4
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "REVISION_SEQUENCE_GAP"):
            run(
                checkpoints=[base],
                revisions=rows,
                stops=[stop(stopLossPrice=D("89"))],
            )

    def test_revision_stop_must_tighten_strictly(self):
        base = checkpoint(
            stop_price=D("90"), best_profit_pct=D("14"),
            armed=True, checkpoint_version=2)
        rows = revisions_for(base)
        rows[1]["stop_price"] = rows[0]["stop_price"]
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "STOP_NOT_STRICTLY_TIGHTER"):
            run(
                checkpoints=[base],
                revisions=rows,
                stops=[stop(stopLossPrice=D("90"))],
            )

    def test_revision_immutable_provenance_drift_blocks(self):
        rows = revisions_for(checkpoint())
        rows[0]["exchange_order_id"] = "999"
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "REVISION_IMMUTABLE_DRIFT"):
            run(revisions=rows)

    def test_final_revision_must_equal_current_checkpoint(self):
        base = checkpoint(
            stop_price=D("90"), best_profit_pct=D("14"),
            armed=True, checkpoint_version=2)
        rows = revisions_for(base)
        rows[-1]["best_profit_pct"] = D("14.1")
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "FINAL_REVISION_MISMATCH"):
            run(
                checkpoints=[base],
                revisions=rows,
                stops=[stop(stopLossPrice=D("90"))],
            )

    def test_verified_replacement_stop_identity_can_recover(self):
        base = checkpoint(
            stop_order_id="100",
            stop_price=D("90"),
            best_profit_pct=D("14"),
            armed=True,
            checkpoint_version=2,
        )
        rows = revisions_for(base)
        rows[0]["stop_order_id"] = "99"
        rows[1]["stop_order_id"] = "100"
        r = run(
            checkpoints=[base],
            revisions=rows,
            amendments=[{
                "state": "VERIFIED",
                "stop_order_id": "99",
                "verified_stop_order_id": "100",
            }],
            stops=[stop(id="100", stopLossPrice=D("90"))],
        )
        self.assertEqual(r.recovered[0].stop_order_id, "100")

    def test_final_revision_stop_identity_must_match_checkpoint(self):
        base = checkpoint(
            stop_order_id="100",
            stop_price=D("90"),
            best_profit_pct=D("14"),
            armed=True,
            checkpoint_version=2,
        )
        rows = revisions_for(base)
        rows[-1]["stop_order_id"] = "101"
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "FINAL_REVISION_STOP_ID_MISMATCH"):
            run(
                checkpoints=[base],
                revisions=rows,
                stops=[stop(id="100", stopLossPrice=D("90"))],
            )

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

    def test_origin_order_count_mismatch_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "ORDER_COUNT_MISMATCH"):
            run(orders=[])

    def test_origin_order_must_match_external_oid(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "ORDER_NOT_FOUND"):
            run(orders=[order(externalOid="other")])

    def test_origin_order_id_drift_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "RECOVERED_ORDER_ID_DRIFT"):
            run(orders=[order(orderId="999")])

    def test_origin_order_symbol_drift_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "RECOVERED_ORDER_SYMBOL_DRIFT"):
            run(orders=[order(symbol="OTHER_USDT")])

    def test_origin_order_side_drift_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "RECOVERED_ORDER_SIDE_DRIFT"):
            run(orders=[order(side=1)])

    def test_origin_order_must_be_final_filled(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "ORDER_NOT_FINAL_FILLED"):
            run(orders=[order(state=2)])

    def test_origin_order_position_id_drift_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "ORDER_POSITION_ID_DRIFT"):
            run(orders=[order(positionId="11")])

    def test_origin_order_volume_drift_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "ORDER_VOLUME_DRIFT"):
            run(orders=[order(dealVol=D("19"))])

    def test_origin_order_average_price_drift_blocks(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "ORDER_AVERAGE_PRICE_DRIFT"):
            run(orders=[order(dealAvgPrice=D("101"))])

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

    def test_entire_position_same_mode_recovers_without_explicit_volume(self):
        r = run(stops=[stop(
            vol=D("0"),
            volType=2,
            profitLossVolType="SAME",
        )])
        self.assertEqual(len(r.recovered), 1)
        self.assertEqual(r.recovered[0].contracts, D("20"))

    def test_separate_mode_requires_stop_loss_volume_on_recovery(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "STOP_VOLUME_DRIFT"):
            run(stops=[stop(
                vol=D("0"),
                volType=2,
                profitLossVolType="SEPARATE",
                stopLossVol=D("0"),
            )])

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


class Ledger:
    def __init__(self):
        self.queries = []
        self.fail = False

    async def fetch(self, query):
        self.queries.append(query)
        if self.fail:
            raise ConnectionError("db down")
        if "t100_live_position_checkpoint_revisions" in query:
            return revisions_for(checkpoint())
        if "t100_live_position_checkpoints" in query:
            return [checkpoint()]
        if "t100_live_order_intents" in query:
            return [intent()]
        if "t100_live_stop_amendments" in query:
            return []
        raise AssertionError("unexpected query")


class Exchange:
    def __init__(self):
        self.positions = [position()]
        self.stops = [stop()]
        self.fail = False
        self.calls = []

    async def order_by_external_oid(self, symbol, external_oid):
        self.calls.append(("order", symbol, external_oid))
        if self.fail:
            raise TimeoutError()
        if symbol != "SIREN_USDT" or external_oid != "t100v1eabc":
            return None
        return order()

    async def open_positions(self, symbol=None):
        self.calls.append(("positions", symbol))
        if self.fail:
            raise TimeoutError()
        return self.positions

    async def open_stop_orders(self, symbol=None):
        self.calls.append(("stops", symbol))
        if self.fail:
            raise TimeoutError()
        return self.stops


class DurableRecoveryGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ledger = Ledger()
        self.exchange = Exchange()
        self.gate = DurableRestartRecoveryGate(self.ledger, self.exchange)

    async def test_select_get_only_recovery_path(self):
        r = await self.gate.inspect_nonflat(
            positions_complete=True, stops_complete=True)
        self.assertEqual(r.mode, "SHADOW_ONLY_RECOVERED_NONFLAT")
        self.assertFalse(r.live_authorization)
        self.assertEqual(len(self.ledger.queries), 4)
        self.assertTrue(all(q.lstrip().startswith("SELECT") for q in self.ledger.queries))
        self.assertEqual(
            self.exchange.calls,
            [
                ("order", "SIREN_USDT", "t100v1eabc"),
                ("positions", None),
                ("stops", None),
            ],
        )

    async def test_completeness_defaults_false(self):
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "COMPLETENESS_UNVERIFIED"):
            await self.gate.inspect_nonflat()

    async def test_ledger_outage_blocks_before_exchange_reads(self):
        self.ledger.fail = True
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "RECOVERY_LEDGER_UNAVAILABLE"):
            await self.gate.inspect_nonflat(
                positions_complete=True, stops_complete=True)
        self.assertEqual(self.exchange.calls, [])

    async def test_exchange_outage_blocks(self):
        self.exchange.fail = True
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "RECOVERY_EXCHANGE_READ_UNAVAILABLE"):
            await self.gate.inspect_nonflat(
                positions_complete=True, stops_complete=True)

    async def test_missing_durable_table_fails_closed(self):
        class MissingTableLedger(Ledger):
            async def fetch(self, query):
                if "t100_live_position_checkpoints" in query:
                    raise RuntimeError("relation does not exist")
                return await super().fetch(query)

        gate = DurableRestartRecoveryGate(MissingTableLedger(), self.exchange)
        with self.assertRaisesRegex(
            DurableRecoveryHalted, "RECOVERY_LEDGER_UNAVAILABLE"):
            await gate.inspect_nonflat(
                positions_complete=True, stops_complete=True)

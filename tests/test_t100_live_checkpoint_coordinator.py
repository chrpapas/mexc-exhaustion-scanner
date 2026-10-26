"""Offline tests for fail-closed checkpoint persistence coordination."""
import unittest
from decimal import Decimal as D

from app.t100_live_checkpoint_coordinator import (
    CheckpointCoordinatorHalted,
    CheckpointPersistenceCoordinator,
)
from app.t100_live_checkpoint_journal import (
    CLOSE_VERIFIED,
    CLOSING_LATCHED,
    REVIEW_LATCHED,
    STOP_VERIFIED,
)
from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_accounting import ShortReconciliation
from app.t100_live_position_accounting import (
    VerifiedClosedPositionAccounting,
)


def cp(**kw):
    data = dict(
        symbol="SIREN_USDT",
        external_oid="t100v1eabc",
        exchange_order_id="100",
        position_id="10",
        contracts=D("20"),
        entry_price=D("100"),
        stop_order_id="99",
        stop_price=D("175"),
        best_profit_pct=D("0"),
        armed=False,
        lifecycle_state="OPEN",
        checkpoint_version=1,
    )
    data.update(kw)
    return PositionCheckpoint(**data)


def intent():
    return {
        "symbol": "SIREN_USDT",
        "external_oid": "t100v1eabc",
        "exchange_order_id": "100",
        "volume_contracts": D("20"),
        "stop_price": D("175"),
        "state": "ACK",
    }


def order():
    return {
        "externalOid": "t100v1eabc",
        "orderId": "100",
        "symbol": "SIREN_USDT",
        "side": 3,
        "state": 3,
        "positionId": "10",
        "dealVol": D("20"),
        "dealAvgPrice": D("100"),
    }


def position():
    return {
        "positionId": "10",
        "symbol": "SIREN_USDT",
        "positionType": 2,
        "state": 1,
        "holdVol": D("20"),
        "holdAvgPrice": D("100"),
    }


def stop(price=D("175"), **kw):
    row = {
        "id": "99",
        "positionId": "10",
        "symbol": "SIREN_USDT",
        "state": 1,
        "stopLossPrice": price,
        "vol": D("20"),
    }
    row.update(kw)
    return row


def amendment(**kw):
    row = {
        "state": "VERIFIED",
        "symbol": "SIREN_USDT",
        "position_id": "10",
        "stop_order_id": "99",
        "verified_stop_order_id": "99",
        "previous_stop": D("175"),
        "proposed_stop": D("90"),
    }
    row.update(kw)
    return row


class Writer:
    def __init__(self):
        self.calls = []
        self.fail = None

    async def create_initial(self, current):
        self.calls.append(("initial", current))
        if self.fail:
            raise self.fail

    async def advance(self, previous, current, *, reason):
        self.calls.append(("advance", previous, current, reason))
        if self.fail:
            raise self.fail


def final_accounting(**kw):
    reconciliation = ShortReconciliation(
        position_id=kw.pop("position_id", "10"),
        opened_contracts=kw.pop("opened_contracts", D("20")),
        closed_contracts=kw.pop("closed_contracts", D("20")),
        remaining_contracts=kw.pop("remaining_contracts", D("0")),
        open_notional_usdt=D("2000"),
        close_notional_usdt=D("1800"),
        realized_gross_usdt=D("200"),
        fees_usdt=D("2"),
        funding_cashflow_usdt=D("1"),
        realized_net_usdt=D("199"),
        status=kw.pop("status", "CLOSED_RECONCILED"),
    )
    data = dict(
        reconciliation=reconciliation,
        entry_order_id="100",
        close_order_id="200",
        stop_order_id="99",
        fill_history_complete=True,
        funding_history_complete=True,
        final_accounting=final_accounting(),
    )
    data.update(kw)
    return VerifiedClosedPositionAccounting(**data)


def gates(**overrides):
    data = dict(
        evidence_fresh=True,
        account_reconciled=True,
        positions_complete=True,
        stops_complete=True,
    )
    data.update(overrides)
    return data


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.writer = Writer()
        self.coordinator = CheckpointPersistenceCoordinator(self.writer)

    async def test_initial_persist_requires_all_evidence_gates(self):
        for key in (
            "evidence_fresh",
            "account_reconciled",
            "positions_complete",
            "stops_complete",
        ):
            with self.subTest(key=key):
                with self.assertRaises(CheckpointCoordinatorHalted):
                    await self.coordinator.persist_initial_protected(
                        intent=intent(),
                        order=order(),
                        positions=[position()],
                        stops=[stop()],
                        **gates(**{key: False}),
                    )
        self.assertEqual(self.writer.calls, [])

    async def test_initial_persist_from_exact_verified_inventory(self):
        current = await self.coordinator.persist_initial_protected(
            intent=intent(),
            order=order(),
            positions=[position()],
            stops=[stop()],
            **gates(),
        )
        self.assertEqual(current, cp())
        self.assertEqual(len(self.writer.calls), 1)
        self.assertEqual(self.writer.calls[0][0], "initial")

    async def test_initial_requires_unique_target_position_and_stop(self):
        with self.assertRaisesRegex(
            CheckpointCoordinatorHalted, "POSITION_NOT_UNIQUE"):
            await self.coordinator.persist_initial_protected(
                intent=intent(), order=order(),
                positions=[position(), position()],
                stops=[stop()], **gates())
        with self.assertRaisesRegex(
            CheckpointCoordinatorHalted, "STOP_NOT_UNIQUE"):
            await self.coordinator.persist_initial_protected(
                intent=intent(), order=order(),
                positions=[position()],
                stops=[stop(), stop()], **gates())

    async def test_verified_stop_persist_requires_complete_reconciled_evidence(self):
        with self.assertRaisesRegex(
            CheckpointCoordinatorHalted, "STOPS_INCOMPLETE"):
            await self.coordinator.persist_verified_stop(
                cp(),
                amendment=amendment(),
                stops=[stop(D("90"))],
                best_profit_pct=D("14"),
                armed=True,
                **gates(stops_complete=False),
            )
        self.assertEqual(self.writer.calls, [])

    async def test_verified_stop_persists_only_after_verified_exchange_state(self):
        current = await self.coordinator.persist_verified_stop(
            cp(),
            amendment=amendment(),
            stops=[stop(D("90"))],
            best_profit_pct=D("14"),
            armed=True,
            **gates(),
        )
        self.assertEqual(current.stop_price, D("90"))
        self.assertEqual(current.checkpoint_version, 2)
        self.assertEqual(self.writer.calls[0][3], STOP_VERIFIED)

    async def test_verified_replacement_stop_id_persists(self):
        current = await self.coordinator.persist_verified_stop(
            cp(),
            amendment=amendment(verified_stop_order_id="100"),
            stops=[stop(D("89"), id="100")],
            best_profit_pct=D("14.5"),
            armed=True,
            **gates(),
        )
        self.assertEqual(current.stop_order_id, "100")
        self.assertEqual(current.stop_price, D("89"))

    async def test_verified_stop_requires_unique_matching_stop(self):
        with self.assertRaisesRegex(
            CheckpointCoordinatorHalted, "VERIFIED_STOP_NOT_UNIQUE"):
            await self.coordinator.persist_verified_stop(
                cp(),
                amendment=amendment(),
                stops=[],
                best_profit_pct=D("14"),
                armed=True,
                **gates(),
            )

    async def test_review_latch_is_fail_closed_db_state_only(self):
        current = await self.coordinator.latch_review(cp())
        self.assertEqual(current.lifecycle_state, "REVIEW")
        self.assertEqual(self.writer.calls[0][3], REVIEW_LATCHED)

    async def test_closing_latch_precedes_any_external_close(self):
        current = await self.coordinator.latch_closing(cp())
        self.assertEqual(current.lifecycle_state, "CLOSING")
        self.assertEqual(self.writer.calls[0][3], CLOSING_LATCHED)

    async def test_close_verified_requires_target_absent_and_provenance(self):
        closing = cp(lifecycle_state="CLOSING", checkpoint_version=2)
        with self.assertRaisesRegex(
            CheckpointCoordinatorHalted, "POSITION_STILL_PRESENT"):
            await self.coordinator.persist_verified_closed(
                closing,
                positions=[position()],
                stops=[],
                final_accounting=final_accounting(),
                **gates(),
            )
        with self.assertRaisesRegex(
            CheckpointCoordinatorHalted, "STOP_STILL_PRESENT"):
            await self.coordinator.persist_verified_closed(
                closing,
                positions=[],
                stops=[stop()],
                final_accounting=final_accounting(),
                **gates(),
            )
        with self.assertRaisesRegex(
            CheckpointCoordinatorHalted, "ACCOUNTING_PROVENANCE_MISMATCH"):
            await self.coordinator.persist_verified_closed(
                closing,
                positions=[],
                stops=[],
                final_accounting=final_accounting(
                    close_provenance_verified=False
                ),
                **gates(),
            )

    async def test_close_requires_typed_final_accounting(self):
        closing = cp(lifecycle_state="CLOSING", checkpoint_version=2)
        with self.assertRaisesRegex(
            CheckpointCoordinatorHalted, "FINAL_ACCOUNTING_UNVERIFIED"
        ):
            await self.coordinator.persist_verified_closed(
                closing,
                positions=[],
                stops=[],
                final_accounting=None,
                **gates(),
            )

    async def test_close_accounting_must_match_checkpoint_identity_and_volume(self):
        closing = cp(lifecycle_state="CLOSING", checkpoint_version=2)
        cases = (
            final_accounting(entry_order_id="999"),
            final_accounting(stop_order_id="100"),
            final_accounting(position_id="11"),
            final_accounting(opened_contracts=D("19")),
            final_accounting(closed_contracts=D("19")),
            final_accounting(remaining_contracts=D("1")),
            final_accounting(status="OPEN_RECONCILED_NOT_CLOSED"),
            final_accounting(fill_history_complete=False),
            final_accounting(funding_history_complete=False),
        )
        for accounting in cases:
            with self.subTest(accounting=accounting):
                with self.assertRaisesRegex(
                    CheckpointCoordinatorHalted,
                    "ACCOUNTING_PROVENANCE_MISMATCH",
                ):
                    await self.coordinator.persist_verified_closed(
                        closing,
                        positions=[],
                        stops=[],
                        final_accounting=accounting,
                        **gates(),
                    )

    async def test_close_verified_persists_closed_checkpoint(self):
        closing = cp(lifecycle_state="CLOSING", checkpoint_version=2)
        current = await self.coordinator.persist_verified_closed(
            closing,
            positions=[],
            stops=[],
            final_accounting=final_accounting(),
            **gates(),
        )
        self.assertEqual(current.lifecycle_state, "CLOSED")
        self.assertEqual(current.checkpoint_version, 3)
        self.assertEqual(self.writer.calls[0][3], CLOSE_VERIFIED)

    async def test_writer_failure_is_wrapped_fail_closed(self):
        from app.t100_live_checkpoint_journal import CheckpointJournalHalted
        self.writer.fail = CheckpointJournalHalted("db failed")
        with self.assertRaisesRegex(
            CheckpointCoordinatorHalted, "PERSISTENCE_BLOCKED"):
            await self.coordinator.persist_initial_protected(
                intent=intent(), order=order(),
                positions=[position()], stops=[stop()],
                **gates(),
            )


if __name__ == "__main__":
    unittest.main()

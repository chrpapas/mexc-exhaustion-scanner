"""Offline tests for crash-safe durable checkpoint persistence protocol."""
import unittest
from dataclasses import replace
from decimal import Decimal as D

from app.t100_live_checkpoint_journal import (
    CHECKPOINTJournalHalted if False else CheckpointJournalHalted,
    CLOSE_VERIFIED,
    CLOSING_LATCHED,
    ENTRY_PROTECTED,
    REVIEW_LATCHED,
    STOP_VERIFIED,
    PostgresCheckpointJournal,
    build_initial_checkpoint,
    build_lifecycle_checkpoint,
    build_verified_stop_checkpoint,
    validate_checkpoint_transition,
)
from app.t100_live_durable_recovery import PositionCheckpoint


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


def intent(**kw):
    data = dict(
        symbol="SIREN_USDT",
        external_oid="t100v1eabc",
        exchange_order_id="100",
        volume_contracts=D("20"),
        stop_price=D("175"),
        state="ACK",
    )
    data.update(kw)
    return data


def order(**kw):
    data = dict(
        externalOid="t100v1eabc",
        orderId="100",
        symbol="SIREN_USDT",
        side=3,
        state=3,
        positionId="10",
        dealVol=D("20"),
        dealAvgPrice=D("100"),
    )
    data.update(kw)
    return data


def position(**kw):
    data = dict(
        positionId="10",
        symbol="SIREN_USDT",
        positionType=2,
        state=1,
        holdVol=D("20"),
        holdAvgPrice=D("100"),
    )
    data.update(kw)
    return data


def stop(**kw):
    data = dict(
        id="99",
        positionId="10",
        symbol="SIREN_USDT",
        state=1,
        stopLossPrice=D("175"),
        vol=D("20"),
    )
    data.update(kw)
    return data


def amendment(**kw):
    data = dict(
        state="VERIFIED",
        symbol="SIREN_USDT",
        position_id="10",
        stop_order_id="99",
        previous_stop=D("175"),
        proposed_stop=D("90"),
    )
    data.update(kw)
    return data


class BuilderTests(unittest.TestCase):
    def test_initial_checkpoint_from_exact_verified_evidence(self):
        x = build_initial_checkpoint(
            intent=intent(), order=order(), position=position(), stop=stop())
        self.assertEqual(x, cp())

    def test_initial_stop_may_already_be_tighter_than_intent(self):
        x = build_initial_checkpoint(
            intent=intent(),
            order=order(),
            position=position(),
            stop=stop(stopLossPrice=D("174")),
        )
        self.assertEqual(x.stop_price, D("174"))

    def test_initial_requires_ack(self):
        with self.assertRaisesRegex(CheckpointJournalHalted, "INTENT_NOT_ACK"):
            build_initial_checkpoint(
                intent=intent(state="UNKNOWN"), order=order(),
                position=position(), stop=stop())

    def test_initial_requires_final_full_short_fill(self):
        cases = [
            (order(side=1), "ORDER_SIDE_DRIFT"),
            (order(state=2), "ORDER_NOT_FINAL_FILLED"),
            (order(dealVol=D("19")), "ORDER_VOLUME_DRIFT"),
        ]
        for bad, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(CheckpointJournalHalted, message):
                    build_initial_checkpoint(
                        intent=intent(), order=bad,
                        position=position(), stop=stop())

    def test_initial_requires_position_identity_quantity_and_price(self):
        cases = [
            (position(positionId="11"), "POSITION_ID_DRIFT"),
            (position(symbol="OTHER_USDT"), "POSITION_SYMBOL_DRIFT"),
            (position(positionType=1), "POSITION_SIDE_DRIFT"),
            (position(state=2), "POSITION_NOT_HOLDING"),
            (position(holdVol=D("19")), "POSITION_VOLUME_DRIFT"),
            (position(holdAvgPrice=D("101")), "POSITION_ENTRY_PRICE_DRIFT"),
        ]
        for bad, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(CheckpointJournalHalted, message):
                    build_initial_checkpoint(
                        intent=intent(), order=order(),
                        position=bad, stop=stop())

    def test_initial_requires_exact_active_protective_volume(self):
        cases = [
            (stop(positionId="11"), "STOP_POSITION_ID_DRIFT"),
            (stop(state=2), "STOP_NOT_ACTIVE"),
            (stop(stopLossPrice=D("176")), "INITIAL_STOP_LOOSER_THAN_INTENT"),
            (stop(vol=D("19")), "STOP_VOLUME_DRIFT"),
        ]
        for bad, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(CheckpointJournalHalted, message):
                    build_initial_checkpoint(
                        intent=intent(), order=order(),
                        position=position(), stop=bad)

    def test_verified_stop_checkpoint_records_actual_tighter_stop(self):
        current = build_verified_stop_checkpoint(
            cp(),
            amendment=amendment(),
            stop=stop(stopLossPrice=D("89")),
            best_profit_pct=D("14.5"),
            armed=True,
        )
        self.assertEqual(current.stop_price, D("89"))
        self.assertEqual(current.best_profit_pct, D("14.5"))
        self.assertTrue(current.armed)
        self.assertEqual(current.checkpoint_version, 2)

    def test_stop_update_requires_verified_matching_amendment(self):
        cases = [
            (amendment(state="UNKNOWN"), "NOT_VERIFIED"),
            (amendment(symbol="OTHER_USDT"), "SYMBOL_DRIFT"),
            (amendment(position_id="11"), "POSITION_ID_DRIFT"),
            (amendment(stop_order_id="100"), "STOP_ID_DRIFT"),
            (amendment(previous_stop=D("174")), "PREVIOUS_STOP_DRIFT"),
            (amendment(proposed_stop=D("175")), "DOES_NOT_TIGHTEN"),
        ]
        for bad, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(CheckpointJournalHalted, message):
                    build_verified_stop_checkpoint(
                        cp(), amendment=bad,
                        stop=stop(stopLossPrice=D("90")),
                        best_profit_pct=D("14"), armed=True)

    def test_stop_update_requires_exchange_stop_tighter_and_same(self):
        cases = [
            (stop(id="100", stopLossPrice=D("90")), "STOP_ID_DRIFT"),
            (stop(state=2, stopLossPrice=D("90")), "STOP_NOT_ACTIVE"),
            (stop(stopLossPrice=D("91")), "STOP_NOT_VERIFIED_TIGHTER"),
            (stop(stopLossPrice=D("175")), "STOP_NOT_VERIFIED_TIGHTER"),
            (stop(stopLossPrice=D("90"), vol=D("19")), "STOP_VOLUME_DRIFT"),
        ]
        for bad, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(CheckpointJournalHalted, message):
                    build_verified_stop_checkpoint(
                        cp(), amendment=amendment(),
                        stop=bad, best_profit_pct=D("14"), armed=True)

    def test_trailing_progress_never_regresses_or_disarms(self):
        previous = cp(
            stop_price=D("90"), best_profit_pct=D("15"),
            armed=True, checkpoint_version=2)
        amend = amendment(
            previous_stop=D("90"), proposed_stop=D("89"))
        with self.assertRaisesRegex(
            CheckpointJournalHalted, "BEST_PROFIT_REGRESSION"):
            build_verified_stop_checkpoint(
                previous, amendment=amend,
                stop=stop(stopLossPrice=D("89")),
                best_profit_pct=D("14.9"), armed=True)
        with self.assertRaisesRegex(
            CheckpointJournalHalted, "ARMED_STATE_CANNOT_DISARM"):
            build_verified_stop_checkpoint(
                previous, amendment=amend,
                stop=stop(stopLossPrice=D("89")),
                best_profit_pct=D("15"), armed=False)

    def test_lifecycle_is_fail_closed(self):
        review = build_lifecycle_checkpoint(cp(), target="REVIEW")
        self.assertEqual(review.lifecycle_state, "REVIEW")
        self.assertEqual(review.checkpoint_version, 2)

        closing = build_lifecycle_checkpoint(review, target="CLOSING")
        self.assertEqual(closing.lifecycle_state, "CLOSING")
        self.assertEqual(closing.checkpoint_version, 3)

        with self.assertRaisesRegex(
            CheckpointJournalHalted, "CLOSE_EVIDENCE_INCOMPLETE"):
            build_lifecycle_checkpoint(closing, target="CLOSED")

        closed = build_lifecycle_checkpoint(
            closing,
            target="CLOSED",
            position_absent=True,
            stop_absent=True,
            close_provenance_verified=True,
        )
        self.assertEqual(closed.lifecycle_state, "CLOSED")
        self.assertEqual(closed.checkpoint_version, 4)


class TransitionValidationTests(unittest.TestCase):
    def test_initial_transition(self):
        validate_checkpoint_transition(
            None, cp(), reason=ENTRY_PROTECTED)

    def test_initial_wrong_reason_or_version_rejected(self):
        with self.assertRaisesRegex(
            CheckpointJournalHalted, "INITIAL_REASON"):
            validate_checkpoint_transition(
                None, cp(), reason=STOP_VERIFIED)
        with self.assertRaisesRegex(
            CheckpointJournalHalted, "INITIAL_CHECKPOINT_STATE_INVALID"):
            validate_checkpoint_transition(
                None, cp(checkpoint_version=2), reason=ENTRY_PROTECTED)

    def test_stop_verified_requires_sequential_tightening(self):
        current = cp(
            stop_price=D("90"), best_profit_pct=D("14"),
            armed=True, checkpoint_version=2)
        validate_checkpoint_transition(
            cp(), current, reason=STOP_VERIFIED)

        with self.assertRaisesRegex(
            CheckpointJournalHalted, "VERSION_NOT_SEQUENTIAL"):
            validate_checkpoint_transition(
                cp(), replace(current, checkpoint_version=3),
                reason=STOP_VERIFIED)
        with self.assertRaisesRegex(
            CheckpointJournalHalted, "STOP_VERIFIED_MUST_TIGHTEN"):
            validate_checkpoint_transition(
                cp(), replace(current, stop_price=D("175")),
                reason=STOP_VERIFIED)

    def test_immutable_entry_and_stop_identity_cannot_change(self):
        current = cp(
            stop_price=D("90"), best_profit_pct=D("14"),
            armed=True, checkpoint_version=2)
        for field, value in (
            ("symbol", "OTHER_USDT"),
            ("external_oid", "other"),
            ("exchange_order_id", "200"),
            ("position_id", "11"),
            ("contracts", D("21")),
            ("entry_price", D("101")),
            ("stop_order_id", "100"),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    CheckpointJournalHalted, "IMMUTABLE_"):
                    validate_checkpoint_transition(
                        cp(), replace(current, **{field: value}),
                        reason=STOP_VERIFIED)

    def test_review_and_closing_do_not_mutate_trading_state(self):
        review = build_lifecycle_checkpoint(cp(), target="REVIEW")
        validate_checkpoint_transition(
            cp(), review, reason=REVIEW_LATCHED)
        with self.assertRaisesRegex(
            CheckpointJournalHalted, "REVIEW_MUST_NOT_MUTATE"):
            validate_checkpoint_transition(
                cp(), replace(review, stop_price=D("174")),
                reason=REVIEW_LATCHED)

        closing = build_lifecycle_checkpoint(cp(), target="CLOSING")
        validate_checkpoint_transition(
            cp(), closing, reason=CLOSING_LATCHED)

    def test_closed_requires_closing_revision(self):
        closing = build_lifecycle_checkpoint(cp(), target="CLOSING")
        closed = build_lifecycle_checkpoint(
            closing, target="CLOSED",
            position_absent=True, stop_absent=True,
            close_provenance_verified=True)
        validate_checkpoint_transition(
            closing, closed, reason=CLOSE_VERIFIED)

    def test_malformed_checkpoint_rejected_at_boundary(self):
        with self.assertRaisesRegex(
            CheckpointJournalHalted, "TRAILING_STATE_INCOHERENT"):
            validate_checkpoint_transition(
                None, cp(best_profit_pct=D("14"), armed=False),
                reason=ENTRY_PROTECTED)


class Tx:
    def __init__(self, conn):
        self.conn = conn
    async def __aenter__(self):
        self.conn.tx_entered += 1
        return self
    async def __aexit__(self, exc_type, exc, tb):
        self.conn.tx_exited += 1
        self.conn.tx_exception_types.append(exc_type)
        return False


class Conn:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.tx_entered = 0
        self.tx_exited = 0
        self.tx_exception_types = []
    def transaction(self):
        return Tx(self)
    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        value = self.results.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class Acquire:
    def __init__(self, conn):
        self.conn = conn
    async def __aenter__(self):
        return self.conn
    async def __aexit__(self, exc_type, exc, tb):
        return False


class Pool:
    def __init__(self, results):
        self.conn = Conn(results)
    def acquire(self):
        return Acquire(self.conn)


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_current_and_revision_are_one_transaction(self):
        pool = Pool(["10", 1])
        journal = PostgresCheckpointJournal(pool)
        await journal.create_initial(cp())
        self.assertEqual(len(pool.conn.calls), 2)
        self.assertIn(
            "t100_live_position_checkpoints",
            pool.conn.calls[0][0])
        self.assertIn(
            "t100_live_position_checkpoint_revisions",
            pool.conn.calls[1][0])
        self.assertEqual(pool.conn.tx_entered, 1)
        self.assertEqual(pool.conn.tx_exited, 1)
        self.assertEqual(pool.conn.tx_exception_types, [None])

    async def test_initial_conflict_aborts_before_revision(self):
        pool = Pool([None])
        journal = PostgresCheckpointJournal(pool)
        with self.assertRaisesRegex(
            CheckpointJournalHalted, "INITIAL_CHECKPOINT_CONFLICT"):
            await journal.create_initial(cp())
        self.assertEqual(len(pool.conn.calls), 1)
        self.assertIs(
            pool.conn.tx_exception_types[0], CheckpointJournalHalted)

    async def test_revision_failure_aborts_transaction(self):
        pool = Pool(["10", None])
        journal = PostgresCheckpointJournal(pool)
        with self.assertRaisesRegex(
            CheckpointJournalHalted, "REVISION_INSERT_FAILED"):
            await journal.create_initial(cp())
        self.assertEqual(len(pool.conn.calls), 2)
        self.assertIs(
            pool.conn.tx_exception_types[0], CheckpointJournalHalted)

    async def test_cas_advance_and_revision_are_atomic(self):
        previous = cp()
        current = cp(
            stop_price=D("90"), best_profit_pct=D("14"),
            armed=True, checkpoint_version=2)
        pool = Pool(["10", 2])
        journal = PostgresCheckpointJournal(pool)
        await journal.advance(
            previous, current, reason=STOP_VERIFIED)
        self.assertEqual(len(pool.conn.calls), 2)
        query, args = pool.conn.calls[0]
        self.assertIn("checkpoint_version=$14", query)
        self.assertEqual(args[-2], 1)
        self.assertEqual(args[-1], "OPEN")
        self.assertEqual(pool.conn.tx_exception_types, [None])

    async def test_cas_failure_never_appends_revision(self):
        previous = cp()
        current = build_lifecycle_checkpoint(
            previous, target="REVIEW")
        pool = Pool([None])
        journal = PostgresCheckpointJournal(pool)
        with self.assertRaisesRegex(
            CheckpointJournalHalted, "CHECKPOINT_CAS_FAILURE"):
            await journal.advance(
                previous, current, reason=REVIEW_LATCHED)
        self.assertEqual(len(pool.conn.calls), 1)

    async def test_database_error_wrapped_fail_closed(self):
        pool = Pool([RuntimeError("db down")])
        journal = PostgresCheckpointJournal(pool)
        with self.assertRaisesRegex(
            CheckpointJournalHalted,
            "INITIAL_CHECKPOINT_TRANSACTION_FAILED"):
            await journal.create_initial(cp())


if __name__ == "__main__":
    unittest.main()

"""Offline immutable V40 closed-position settlement persistence tests.

No database network, account credentials or exchange order mutation.
"""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.t100_live_accounting import ShortReconciliation
from app.t100_live_position_accounting import (
    PositionAccountingHalted, PostgresFinalAccountingLedger,
    VerifiedClosedPositionAccounting,
)


class FakeFinalLedger:
    def __init__(self):
        self.rows = {}
        self.inserts = 0
        self.reads = 0
        self.fail_read = False
        self.transaction_isolation = None

    def acquire(self):
        return self

    def transaction(self, *, isolation):
        self.transaction_isolation = isolation
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def fetchval(self, sql, *values):
        assert "ON CONFLICT (position_id) DO NOTHING" in sql
        assert "INSERT INTO public.t100_live_final_position_accounting" in sql
        self.inserts += 1
        fields = PostgresFinalAccountingLedger._FIELDS
        row = dict(zip(fields, values))
        if values[0] in self.rows:
            return None
        if any(existing["entry_order_id"] == row["entry_order_id"]
               or existing["close_order_id"] == row["close_order_id"]
               for existing in self.rows.values()):
            raise RuntimeError("SECRET existing order already has a settlement")
        self.rows[values[0]] = row
        return values[0]

    async def fetchrow(self, sql, position_id):
        assert "FOR SHARE" in sql
        self.reads += 1
        if self.fail_read:
            raise TimeoutError("SECRET SQL outcome uncertain")
        return self.rows.get(position_id)


def accounting():
    r = ShortReconciliation(
        position_id="10", opened_contracts=D("2"),
        closed_contracts=D("2"), remaining_contracts=D("0"),
        open_notional_usdt=D("200"), close_notional_usdt=D("180"),
        realized_gross_usdt=D("20"), fees_usdt=D("0.2"),
        funding_cashflow_usdt=D("1"), realized_net_usdt=D("20.8"),
        status="CLOSED_RECONCILED",
    )
    return VerifiedClosedPositionAccounting(
        reconciliation=r,
        entry_order_id="100", close_order_id="200", stop_order_id="99",
    )


@pytest.mark.asyncio
async def test_exact_full_row_is_persisted_once_and_replay_is_idempotent():
    pool = FakeFinalLedger()
    writer = PostgresFinalAccountingLedger(pool)
    proof = accounting()
    assert await writer.persist(proof) == "PERSISTED"
    assert await writer.persist(proof) == "ALREADY_VERIFIED"
    assert pool.transaction_isolation == "read_committed"
    assert pool.inserts == 2
    assert pool.reads == 2
    assert pool.rows["10"]["realized_net_usdt"] == D("20.8")


@pytest.mark.asyncio
async def test_same_net_cannot_hide_changed_fee_or_funding_components():
    pool = FakeFinalLedger()
    writer = PostgresFinalAccountingLedger(pool)
    original = accounting()
    assert await writer.persist(original) == "PERSISTED"
    modified = replace(
        original, reconciliation=replace(
            original.reconciliation, fees_usdt=D("0.4"),
            funding_cashflow_usdt=D("1.2"),
            # net is identical despite changed underlying economics.
            realized_net_usdt=D("20.8"),
        ),
    )
    with pytest.raises(PositionAccountingHalted, match="IMMUTABILITY_MISMATCH"):
        await writer.persist(modified)
    assert pool.rows["10"]["fees_usdt"] == D("0.2")
    assert pool.rows["10"]["funding_cashflow_usdt"] == D("1")


@pytest.mark.asyncio
async def test_incomplete_close_or_history_never_touches_database():
    pool = FakeFinalLedger()
    writer = PostgresFinalAccountingLedger(pool)
    base = accounting()
    bad = (
        replace(base, fill_history_complete=False),
        replace(base, funding_history_complete=False),
        replace(base, close_provenance_verified=False),
        replace(base, live_authorization=True),
        replace(base, reconciliation=replace(
            base.reconciliation, remaining_contracts=D("1"))),
        replace(base, reconciliation=replace(
            base.reconciliation, status="PARTIALLY_CLOSED")),
        replace(base, reconciliation=replace(
            base.reconciliation, closed_contracts=D("1"))),
        replace(base, reconciliation=replace(
            base.reconciliation, fees_usdt=D("NaN"))),
    )
    for proof in bad:
        with pytest.raises(PositionAccountingHalted):
            await writer.persist(proof)
    assert pool.inserts == pool.reads == 0


@pytest.mark.asyncio
async def test_duplicate_entry_or_close_cannot_be_reused_for_other_position():
    pool = FakeFinalLedger()
    writer = PostgresFinalAccountingLedger(pool)
    base = accounting()
    assert await writer.persist(base) == "PERSISTED"
    different_position = replace(
        base, reconciliation=replace(base.reconciliation, position_id="11"),
        close_order_id="201",
    )
    with pytest.raises(PositionAccountingHalted,
                       match="SQL_OUTCOME_UNKNOWN_RECONCILE_ONLY"):
        await writer.persist(different_position)
    assert set(pool.rows) == {"10"}


@pytest.mark.asyncio
async def test_sql_readback_unknown_is_never_reported_as_committed():
    pool = FakeFinalLedger()
    pool.fail_read = True
    with pytest.raises(PositionAccountingHalted,
                       match="SQL_OUTCOME_UNKNOWN_RECONCILE_ONLY") as exc:
        await PostgresFinalAccountingLedger(pool).persist(accounting())
    assert "SECRET" not in str(exc.value)


@pytest.mark.asyncio
async def test_missing_transaction_pool_fails_before_any_sql():
    with pytest.raises(PositionAccountingHalted,
                       match="RESTRICTED_POOL_REQUIRED"):
        PostgresFinalAccountingLedger(object())

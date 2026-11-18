"""Exact before-POST V40 contract multiplier archive readback, offline only."""
from dataclasses import replace
from datetime import datetime,timezone,timedelta
from decimal import Decimal as D

import pytest

from app.t100_v40_at_entry_contract_specs import (
    AT_ENTRY_SPEC_SQL,PostgresV40AtEntryContractSpecReader,
    V40EntryContractArchiveHalted,
)
from tests.test_t100_live_checkpoint_journal import cp


def spec(**changes):
    row=dict(
        signal_id=42,external_oid="t100v1eabc",symbol="SIREN_USDT",
        contract_size=D("1"),volume_step=D("1"),price_tick=D("1"),
        reference_price=D("100"),notional_usdt=D("200"),
        volume_contracts=D("2"),stop_price=D("175"),
        ledger_sha256="a"*64,inventory_sha256="b"*64,
        captured_at=datetime(2026,1,1,tzinfo=timezone.utc),
    )
    row.update(changes)
    return row


class FakePool:
    def __init__(self,first,second=None,*,error=False):
        self.rows=(first,first if second is None else second)
        self.error=error
        self.reads=0
        self.transactions=[]

    def acquire(self):return self
    async def __aenter__(self):return self
    async def __aexit__(self,*args):return False

    def transaction(self,*,isolation,readonly):
        self.transactions.append((isolation,readonly))
        return self

    async def fetchrow(self,sql,oid):
        assert sql==AT_ENTRY_SPEC_SQL and oid=="t100v1eabc"
        if self.error:raise RuntimeError("SECRET DB ERROR")
        row=self.rows[self.reads]
        self.reads+=1
        return row


@pytest.mark.asyncio
async def test_exact_spec_is_read_from_two_independent_readonly_transactions():
    pool=FakePool(spec())
    out=await PostgresV40AtEntryContractSpecReader(pool).read(
        cp(contracts=D("2")),
    )
    assert out.read_twice_verified is True
    assert out.contract_size==D("1")
    assert out.initial_stop_price==D("175")
    assert out.volume_contracts==D("2")
    assert pool.reads==2
    assert pool.transactions==[("repeatable_read",True)]*2
    assert out.database_writes==out.mexc_posts==0
    assert out.live_authorization is False


@pytest.mark.asyncio
async def test_unknown_spec_and_changed_spec_do_not_certify():
    for pool,code in (
        (FakePool(None),"MISSING_BEFORE_POST_SPEC"),
        (FakePool(spec(),spec(contract_size=D("2"))),
         "CHANGED_BETWEEN_TRANSACTIONS"),
        (FakePool(spec(),error=True),"READONLY_SQL_UNVERIFIED"),
    ):
        with pytest.raises(V40EntryContractArchiveHalted,match=code) as err:
            await PostgresV40AtEntryContractSpecReader(pool).read(
                cp(contracts=D("2")),
            )
        assert "SECRET" not in str(err.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes",[
    dict(volume_contracts=D("3")),
    dict(stop_price=D("174")),
    dict(notional_usdt=D("201")),
    dict(ledger_sha256="forged"),
    dict(contract_size=D("NaN")),
    dict(captured_at=datetime.now(timezone.utc)+timedelta(hours=1)),
])
async def test_nonimmutable_or_inconsistent_terms_are_rejected(changes):
    with pytest.raises(V40EntryContractArchiveHalted):
        await PostgresV40AtEntryContractSpecReader(
            FakePool(spec(**changes)),
        ).read(cp(contracts=D("2")))

"""Fake-transaction tests for the reservation protocol; no DB or exchange required."""
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from dataclasses import replace
from app.t100_live_vol30 import Vol30H4Planner, Vol30MarketSnapshot
from app.t100_live_vol30_store import PostgresVol30Reservations

NOW = datetime.now(timezone.utc).replace(minute=0,second=1,microsecond=0)
if NOW.minute not in (0,30):
    raise RuntimeError('bad test fixture')

class FakeDB:
    def __init__(self):
        self.children=[]
        self.parent={
            'signal_id':501,'symbol':'SIREN_USDT','tier':'HIGH_225',
            'admitted_at':NOW-timedelta(minutes=5),'admitted_equity_usdt':D(10000),
            'target_notional_usdt':D(5625),'status':'ACTIVE'
        }
        self.advisory=[]
        self.active_other_parents=0
    @asynccontextmanager
    async def acquire(self):yield self
    @asynccontextmanager
    async def transaction(self):yield self
    async def fetchrow(self,sql,*args):return self.parent
    async def fetchval(self,sql,*args):
        if 'COUNT(*) FROM t100_live_vol30_parents' in sql:
            return self.active_other_parents
        if 'SUM(reserved_notional_usdt)' in sql:
            return sum((r['reserved_notional_usdt'] for r in self.children
                if r['turnover_window_end']==args[1]),D(0))
        raise AssertionError(sql)
    async def fetch(self,sql,*args):return self.children
    async def execute(self,sql,*args):
        if 'pg_advisory_xact_lock' in sql:
            self.advisory.append(args[0]);return
        if 'INSERT INTO t100_live_vol30_children' in sql:
            sig,seq,sym,oid,window,turnover,notional,volume=args
            self.children.append(dict(child_seq=seq,state='PREPARED',
                turnover_window_end=window,reserved_notional_usdt=notional,
                filled_notional_usdt=D(0),external_oid=oid))
            return
        raise AssertionError(sql)

def snap():
    return Vol30MarketSnapshot(
        NOW,NOW-timedelta(milliseconds=200),NOW-timedelta(milliseconds=100),
        NOW.replace(minute=0,second=0,microsecond=0),D(20000),D(0),
        D(2),D('2.001'),D(1),D(1),D(1),D(10000),[['2','10000']],
        D(10000),D(10000),D(0),D(0),D(10000),D(10000))

class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_reservation_and_no_second_inflight(self):
        db=FakeDB()
        store=PostgresVol30Reservations(db)
        # Move market timestamps to actual now without letting timestamp second drift.
        s=snap()
        live=datetime.now(timezone.utc)
        s=replace(s,observed_at=live,book_exchange_at=live-timedelta(milliseconds=200),
                  book_received_at=live-timedelta(milliseconds=100),
                  turnover_window_end=live.replace(minute=(live.minute//30)*30,second=0,microsecond=0))
        d,r=await store.reserve(501,s,Vol30H4Planner(daily_loss_limit_usdt=D(100)))
        self.assertEqual(d.action,'SUBMIT_CHILD')
        self.assertEqual(r.reserved_notional_usdt,D(1000))
        self.assertEqual(len(db.children),1)
        d2,r2=await store.reserve(501,s,Vol30H4Planner(daily_loss_limit_usdt=D(100)))
        self.assertIsNone(r2)
        self.assertEqual(d2.reason,'PREVIOUS_CHILD_REQUIRES_EXCHANGE_RECONCILIATION')
        self.assertEqual(len(db.advisory),2)

    async def test_four_other_live_parents_defer_new_entry(self):
        db=FakeDB(); db.active_other_parents=4
        store=PostgresVol30Reservations(db)
        live=datetime.now(timezone.utc)
        s=replace(snap(),observed_at=live,book_exchange_at=live-timedelta(milliseconds=200),
                  book_received_at=live-timedelta(milliseconds=100),
                  turnover_window_end=live.replace(minute=(live.minute//30)*30,second=0,microsecond=0))
        d,r=await store.reserve(501,s,Vol30H4Planner(daily_loss_limit_usdt=D(100)))
        self.assertEqual(d.reason,'ALL_FOUR_SLOTS_OCCUPIED')
        self.assertIsNone(r)

    async def test_parent_rejects_post_restart_missing_target(self):
        db=FakeDB();db.parent['target_notional_usdt']=D(100000)
        store=PostgresVol30Reservations(db)
        live=datetime.now(timezone.utc)
        s=replace(snap(),observed_at=live,book_exchange_at=live-timedelta(milliseconds=200),
                  book_received_at=live-timedelta(milliseconds=100),
                  turnover_window_end=live.replace(minute=(live.minute//30)*30,second=0,microsecond=0))
        with self.assertRaisesRegex(ValueError,'frozen strategy'):
            await store.reserve(501,s,Vol30H4Planner(daily_loss_limit_usdt=D(100)))
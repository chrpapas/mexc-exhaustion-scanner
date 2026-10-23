"""Transactional reservations for H4 child-order budgets (no network, no orders).

Do not call any exchange POST directly after reserve(): this store only creates a
PREPARED reservation. A supervised execution coordinator is still required for
post-refresh rechecks, safe order submission and independently verified fills.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal

from app.t100_live_vol30 import (
    Vol30Parent, Vol30MarketSnapshot, Vol30H4Planner, ChildDecision,
)


@dataclass(frozen=True)
class ReservedChild:
    external_oid: str
    signal_id: int
    child_seq: int
    symbol: str
    volume_contracts: Decimal
    reserved_notional_usdt: Decimal
    turnover_window_end: datetime


class PostgresVol30Reservations:
    """PG advisory lock + row-lock prevents overlapping budgets across Render workers."""
    def __init__(self, pool):
        self.pool = pool

    async def reserve(self, signal_id: int, snap: Vol30MarketSnapshot,
                      planner: Vol30H4Planner) -> tuple[ChildDecision, ReservedChild | None]:
        # Exchange quotes must be fresh at DB reservation time, not only against
        # user-supplied observed_at. Strict 2s maximum clock skew in either direction.
        age_s = (datetime.now(timezone.utc) - snap.observed_at.astimezone(timezone.utc)).total_seconds()
        if not -0.25 <= age_s <= 2.0:
            raise RuntimeError('SNAPSHOT_STALE_AT_DB_RESERVATION')

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                base = await conn.fetchrow(
                    'SELECT * FROM t100_live_vol30_parents WHERE signal_id=$1', signal_id)
                if not base:
                    raise RuntimeError('UNKNOWN_LIVE_PARENT')
                symbol = str(base['symbol'])
                # Symbol-level advisory lock makes turnover accounting atomic
                # across multiple parents and all simultaneous Render instances.
                await conn.execute('SELECT pg_advisory_xact_lock(hashtext($1))',
                                   f't100_live_vol30:{symbol}')
                base = await conn.fetchrow(
                    'SELECT * FROM t100_live_vol30_parents WHERE signal_id=$1 FOR UPDATE', signal_id)
                if base['status'] != 'ACTIVE':
                    raise RuntimeError('LIVE_PARENT_NOT_ACTIVE')

                # All reservations count against historical participation,
                # including unknown/cancelled attempts: pessimistic but safe.
                used = Decimal(str(await conn.fetchval(
                    '''SELECT COALESCE(SUM(reserved_notional_usdt),0)
                       FROM t100_live_vol30_children
                       WHERE symbol=$1 AND turnover_window_end=$2''',
                    symbol, snap.turnover_window_end)))
                records = await conn.fetch(
                    '''SELECT child_seq,state,reserved_notional_usdt,filled_notional_usdt
                       FROM t100_live_vol30_children WHERE signal_id=$1
                       ORDER BY child_seq FOR UPDATE''', signal_id)
                filled = sum((Decimal(str(r['filled_notional_usdt'])) for r in records), Decimal(0))
                inflight = {'PREPARED','SUBMITTING','UNKNOWN','ACK','PARTIAL','HALTED'}
                pending = sum((Decimal(str(r['reserved_notional_usdt'])) -
                               Decimal(str(r['filled_notional_usdt']))
                               for r in records if r['state'] in inflight), Decimal(0))
                other_active_parents = int(await conn.fetchval(
                    '''SELECT COUNT(*) FROM t100_live_vol30_parents
                       WHERE signal_id<>$1 AND status IN ('ACTIVE','HALTED')''', signal_id))
                original = Vol30Parent(
                    signal_id=int(base['signal_id']),symbol=symbol,tier=str(base['tier']),
                    admitted_at=base['admitted_at'],
                    admitted_equity_usdt=Decimal(str(base['admitted_equity_usdt'])),
                    target_notional_usdt=Decimal(str(base['target_notional_usdt'])),
                    filled_notional_usdt=filled,pending_notional_usdt=pending,
                    other_open_positions=other_active_parents)
                if any(r['state'] in inflight for r in records):
                    return (ChildDecision('DEFER','PREVIOUS_CHILD_REQUIRES_EXCHANGE_RECONCILIATION'), None)
                # Portfolio cap and open slots are passed in an independently
                # verified real-account snapshot, NEVER the paper ledger.
                market = replace(snap, bucket_committed_notional_usdt=used)
                decision = planner.decide(original, market)
                if decision.action != 'SUBMIT_CHILD':
                    return decision, None
                seq = max((int(r['child_seq']) for r in records), default=0) + 1
                oid = 't100v2c'+hashlib.sha256(f'{signal_id}:{symbol}:{seq}'.encode()).hexdigest()[:20]
                await conn.execute(
                    '''INSERT INTO t100_live_vol30_children
                       (signal_id,child_seq,symbol,external_oid,turnover_window_end,
                        prior30_turnover_usdt,reserved_notional_usdt,volume_contracts)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8)''',
                    signal_id,seq,symbol,oid,snap.turnover_window_end,
                    snap.prior30_turnover_usdt,decision.notional_usdt,
                    decision.contracts)
                return decision, ReservedChild(oid,signal_id,seq,symbol,
                    decision.contracts, decision.notional_usdt, snap.turnover_window_end)

    async def transition_child(self, oid: str, expected: str, new_state: str,
                               *, order_id: str | None = None,
                               verified_filled_usdt: Decimal | None = None) -> bool:
        transitions = {
            'PREPARED': {'SUBMITTING','CANCELLED'},
            'SUBMITTING': {'UNKNOWN','ACK','HALTED'},
            'UNKNOWN': {'ACK','HALTED'},
            'ACK': {'PARTIAL','VERIFIED_FILLED','UNKNOWN','HALTED'},
            'PARTIAL': {'VERIFIED_FILLED','HALTED'},
            'HALTED': {'VERIFIED_FILLED','CANCELLED'},
        }
        if new_state not in transitions.get(expected,set()):
            raise ValueError('ILLEGAL_CHILD_STATE_TRANSITION')
        if new_state == 'VERIFIED_FILLED' and verified_filled_usdt is None:
            raise ValueError('verified fill value required')
        async with self.pool.acquire() as conn:
            # One conditional UPDATE: prevents ambiguous duplicate writes.
            row = await conn.fetchval(
                '''UPDATE t100_live_vol30_children SET state=$3,
                   exchange_order_id=COALESCE($4,exchange_order_id),
                   filled_notional_usdt=COALESCE($5,filled_notional_usdt),updated_at=now()
                   WHERE external_oid=$1 AND state=$2 RETURNING id''',
                oid,expected,new_state,order_id,verified_filled_usdt)
            return row is not None
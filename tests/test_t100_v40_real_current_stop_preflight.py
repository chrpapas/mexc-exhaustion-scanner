"""Genuine completed Min30 trailing preflight with signed full-account bookends.

No MEXC POST or PostgreSQL writes; the tests inject the exact public ticker,
immutable checkpoint, fully protective native stop and causal bar.
"""
from dataclasses import asdict,replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult,LedgerSnapshot,_ledger_digest,_venue_digest,
)
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_mexc_readonly import (
    FourSlotMexcReadVenue,FourSlotMexcCompleteness,
)
from app.t100_v40_owned_completed_bar_reader import (
    PostgresV40OwnedBarReader,V40OwnedDueBarWindow,
)
from app.t100_v40_unreleased_bar_cursor import V40BarCursorState
from app.t100_v40_unreleased_exit_policy import V40CompletedBar
from app import t100_v40_real_current_stop_preflight as m
from tests.test_t100_live_trailing_preflight import (
    checkpoint, pos, stop, report,
)

OPEN=1_800_000_000
NOW_MS=(OPEN+1808)*1000
PID="52"
SYM="SIREN_USDT"

class Public:
    def __init__(self):
        self.bad=False
        self.calls=[]
    async def get(self,path,*,params):
        self.calls.append((path,params))
        assert path=="/api/v1/contract/ticker"
        assert params=={"symbol":SYM}
        class Resp:
            def raise_for_status(self):pass
            def json(other):
                return {
                    "success":True,"code":0,
                    "data":{"symbol":SYM,"lastPrice":"86.1",
                            "timestamp":NOW_MS-1200},
                }
        if self.bad:
            class Bad(Resp):
                def json(other):
                    return {"success":True,"code":0,
                            "data":{"symbol":SYM,"lastPrice":"9999",
                                    "timestamp":NOW_MS-1200}}
            return Bad()
        return Resp()

def make():
    cp=checkpoint(SYM,PID,"92")
    cp=replace(cp,external_oid="t100v1epreflight")
    ledger=LedgerSnapshot([asdict(cp)],[],[],[],True)
    positions=[pos(cp)]
    stops=[stop(cp)]
    snapshot=FourSlotSnapshotResult(
        "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
        occupied_slots=1,recovery=SimpleNamespace(recovered=(cp,)),
        ledger_hash=_ledger_digest(ledger),
        critical_inventory_hash=_venue_digest(positions,stops),
    )
    bar=V40CompletedBar(OPEN,D("100"),D("105"),D("86"),D("86.1"))
    cursor=V40BarCursorState(
        position_id=PID,entry_external_oid=cp.external_oid,
        signed_fill_at_ms=(OPEN-1200)*1000,
        last_committed_open_ts=None,
        pending_open_ts=None,pending_status=None,
        pending_stop_price=None,pending_best_profit_pct=None,
        pending_trail_armed=None,checkpoint_version=1,
        stop_order_id=cp.stop_order_id,confirmed_stop_price=cp.stop_price,
        best_profit_pct=D("0"),trail_armed=False,cursor_revision=1,
    )
    window=V40OwnedDueBarWindow(
        "V40_OWNED_MIN30_DUE_CANDLES_VERIFIED_NONAUTHORIZING",
        cursors=(cursor,),bars={SYM:bar},ticks={SYM:D(".01")},
        no_due_position_ids=(),latest_safe_open_ts=OPEN,
        scanner_role_verified=True,runtime_role_verified=True,
        exchange_public_specs_observed=True,
    )
    read=object.__new__(PostgresFourSlotLedgerReader)
    async def read_snapshot():
        return ledger
    read.read_snapshot=read_snapshot
    venue=object.__new__(FourSlotMexcReadVenue)
    async def open_positions():return [dict(x) for x in positions]
    async def open_stops():return [dict(x) for x in stops]
    venue.open_positions=open_positions
    venue.open_stop_orders=open_stops
    complete=object.__new__(FourSlotMexcCompleteness)
    async def inspect_counts():return report(1)
    complete.inspect=inspect_counts
    source=object.__new__(PostgresV40OwnedBarReader)
    async def observe(**kw):
        assert kw["position_ids"]==(PID,)
        return window
    source.observe=observe
    snapshots=SimpleNamespace()
    async def inspect():return snapshot
    snapshots.inspect=inspect
    clock=[NOW_MS-1900,NOW_MS-1500,NOW_MS-1000,NOW_MS-400,NOW_MS]
    def now():
        return clock.pop(0) if clock else NOW_MS
    p=object.__new__(m.V40RealCurrentStopPreflight)
    p.ledger=read;p.snapshots=snapshots;p.venue=venue
    p.completeness=complete;p.owned_bars=source
    pub=Public();p.public_get=pub;p.now_ms=now
    async def signal_id(_):return 42
    p._owned_signal_id=signal_id
    return SimpleNamespace(
        port=p,snapshot=snapshot,positions=positions,stops=stops,
        window=window,ledger=ledger,public=pub,cp=cp,
    )

@pytest.mark.asyncio
async def test_exact_causal_closed_bar_and_current_signed_account_enables_one_preflight():
    x=make()
    out=await x.port.observe(PID)
    assert out.target_signal_id==42
    assert out.checkpoints==(x.cp,)
    assert out.preflight.status=="TRAILING_PROPOSAL_OBSERVED_NONAUTHORIZING"
    assert out.preflight.proposal.position_id==PID
    assert out.preflight.proposal.proposed_stop==D("86.50")
    assert out.preflight.proposal.best_profit_pct==D("14")
    assert out.preflight.proposal.armed is True
    assert out.preflight.market_low_provenance_verified is True
    assert out.preflight.signed_get_provenance_independently_verified is True
    assert out.preflight.live_stop_mutation_allowed is False
    assert out.preflight.live_authorization is False
    assert x.public.calls==[("/api/v1/contract/ticker",{"symbol":SYM})]

@pytest.mark.asyncio
@pytest.mark.parametrize("fault",[
    "wrong_position","last_price_crossed","changed_checkpoint",
    "pending_bar","wrong_stop_bookend","wrong_slots",
])
async def test_real_stop_preflight_halts_on_stale_incomplete_or_unprotected(fault):
    x=make()
    if fault=="wrong_position":
        pid="9999"
    else:
        pid=PID
    if fault=="last_price_crossed":
        x.public.bad=True
    elif fault=="changed_checkpoint":
        async def wrong():
            return replace(x.ledger,checkpoints=[])
        x.port.ledger.read_snapshot=wrong
    elif fault=="pending_bar":
        x.window=replace(
            x.window,cursors=(
                replace(x.window.cursors[0],pending_open_ts=OPEN),
            ),
        )
        async def pending(**kwargs):return x.window
        x.port.owned_bars.observe=pending
    elif fault=="wrong_stop_bookend":
        x.stops[0]["stopLossPrice"]="123"
    elif fault=="wrong_slots":
        async def wrong_snapshot():
            return replace(x.snapshot,occupied_slots=4)
        x.port.snapshots.inspect=wrong_snapshot
    with pytest.raises(m.V40RealStopPreflightHalted):
        await x.port.observe(pid)

def test_constructor_requires_same_account_snapshot_bar_and_signed_venue():
    import types
    bad=object.__new__(PostgresFourSlotLedgerReader)
    with pytest.raises(m.V40RealStopPreflightHalted):
        m.V40RealCurrentStopPreflight(
            ledger=bad,snapshots=object(),venue=object(),
            completeness=object(),owned_bars=object(),
            public_get=Public(),now_ms=lambda:NOW_MS,
        )

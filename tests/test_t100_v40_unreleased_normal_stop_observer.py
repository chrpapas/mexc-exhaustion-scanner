"""Flat account alone never means V40 stop-loss execution."""
from dataclasses import replace
from decimal import Decimal as D
import pytest
from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_v40_unreleased_normal_stop_observer import (
    observe_one_normal_v40_native_stop_exit, V40NormalStopObservationHalted,
)


class Client:
    def __init__(self, positions=()):
        self.positions=list(positions)
        self.calls=[]
    async def _private_get(self, path, params=None):
        self.calls.append(path)
        if path=="/api/v1/private/position/open_positions":
            return self.positions
        if path=="/api/v1/private/stoporder/open_orders":
            return []
        if path=="/api/v1/private/order/open_order_total_count":
            return dict(sumCount=0,stopOrderCount=0,limitOrderCount=0,
                        planOrderCount=0,trackOrderCount=0)
        if path=="/api/v1/private/position/position_mode":
            return {"positionMode":1}
        if path=="/api/v1/private/stoporder/list/orders":
            return []
        raise AssertionError("Unexpected private GET: "+path)


def cp():
    return PositionCheckpoint(
        symbol="EXAMPLE_USDT",external_oid="entry",
        exchange_order_id="110",position_id="111",
        contracts=D("1"),entry_price=D("100"),
        stop_order_id="112",stop_price=D("175"),
        best_profit_pct=D("0"),armed=False,
        lifecycle_state="OPEN",checkpoint_version=1,
    )


async def no_delay(_):
    pass


@pytest.mark.asyncio
async def test_flat_but_no_finished_sl_never_reports_exit():
    client=Client()
    observed=await observe_one_normal_v40_native_stop_exit(
        probe=ReadOnlyMexcProbe(client),checkpoint=cp(),wait=no_delay,
    )
    assert observed.status=="V40_NORMAL_STOP_HISTORY_OR_FILLS_UNVERIFIED_HALT"
    assert observed.closing_fills is None
    assert observed.exchange_posts==0
    assert observed.db_writes==0


@pytest.mark.asyncio
async def test_unprotected_nonflat_is_not_successful_exit():
    client=Client([{"positionId":"111","symbol":"EXAMPLE_USDT"}])
    observed=await observe_one_normal_v40_native_stop_exit(
        probe=ReadOnlyMexcProbe(client),checkpoint=cp(),wait=no_delay,
    )
    assert observed.status=="V40_NORMAL_STOP_NONFLAT_OR_UNPROTECTED_GET_HALT"
    assert observed.stop_provenance is None


@pytest.mark.asyncio
async def test_invalid_stop_identity_aborts_before_any_get():
    client=Client()
    with pytest.raises(V40NormalStopObservationHalted,match="ORIGIN_INVALID"):
        await observe_one_normal_v40_native_stop_exit(
            probe=ReadOnlyMexcProbe(client),
            checkpoint=replace(cp(),stop_order_id="missing"),
            wait=no_delay,
        )
    assert client.calls==[]

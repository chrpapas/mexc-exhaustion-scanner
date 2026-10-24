"""Offline double-read consistency checks using synthetic account snapshots."""
import unittest
from app.t100_live_consistency import (
    InventoryRead, double_read_inventory, double_read_raw_inventory,
    ConsistencyHalted,
)


def read(t=1000, positions=None, stops=None, **kw):
    return InventoryRead(
        positions=[] if positions is None else positions,
        stops=[] if stops is None else stops,
        observed_at_ms=t,
        positions_complete=kw.get("positions_complete", True),
        stops_complete=kw.get("stops_complete", True),
    )


def pos(vol="2"):
    return dict(symbol="SIREN_USDT", positionId="1", positionType=2, holdVol=vol, state=1)


def stop(price="175"):
    return dict(id="9", positionId="1", stopLossPrice=price, vol="2", state=1)


class Reader:
    def __init__(self, *observations):
        self.observations=list(observations)
        self.calls=0
    async def __call__(self):
        self.calls += 1
        return self.observations.pop(0)


async def verify(*observations, now=1800, **kw):
    reader=Reader(*observations)
    return await double_read_inventory(reader, now_ms=lambda: now, **kw)


class ConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_flat_double_read(self):
        r=await verify(read(1000),read(1500))
        self.assertEqual(r.mode,"SHADOW_ONLY")
        self.assertEqual((r.positions,r.stops),(0,0))

    async def test_matching_protected_inventory(self):
        r=await verify(read(1000,[pos()],[stop()]),
                       read(1500,[pos()],[stop()]))
        self.assertEqual((r.positions,r.stops),(1,1))
        self.assertEqual(len(r.fingerprint_sha256),64)

    async def test_row_order_does_not_change_fingerprint(self):
        a=[pos(),dict(symbol="OTHER_USDT",positionId="2",positionType=1,holdVol="1",state=1)]
        r=await verify(read(1000,a,[stop()]),read(1500,list(reversed(a)),[stop()]))
        self.assertEqual(r.positions,2)

    async def test_quantity_drift_blocks(self):
        with self.assertRaisesRegex(ConsistencyHalted,"INVENTORY_CHANGED"):
            await verify(read(1000,[pos()],[stop()]),
                         read(1500,[pos("3")],[stop()]))

    async def test_stop_price_drift_blocks(self):
        with self.assertRaisesRegex(ConsistencyHalted,"INVENTORY_CHANGED"):
            await verify(read(1000,[pos()],[stop()]),
                         read(1500,[pos()],[stop("176")]))

    async def test_stale_snapshot_blocks(self):
        with self.assertRaisesRegex(ConsistencyHalted,"DOUBLE_READ_UNVERIFIED"):
            await verify(read(1000),read(1500),now=4000)

    async def test_partial_inventory_blocks(self):
        with self.assertRaisesRegex(ConsistencyHalted,"DOUBLE_READ_UNVERIFIED"):
            await verify(read(1000),read(1500,stops_complete=False))

    async def test_duplicate_position_blocks(self):
        with self.assertRaisesRegex(ConsistencyHalted,"DOUBLE_READ_UNVERIFIED"):
            await verify(read(1000,[pos(),pos()]),read(1500,[pos(),pos()]))

    async def test_duplicate_stop_blocks(self):
        with self.assertRaisesRegex(ConsistencyHalted,"DOUBLE_READ_UNVERIFIED"):
            await verify(read(1000,stops=[stop(),stop()]),read(1500,stops=[stop(),stop()]))

    async def test_repeated_timestamp_blocks(self):
        with self.assertRaisesRegex(ConsistencyHalted,"NOT_CONSECUTIVE"):
            await verify(read(1000),read(1000))

    async def test_backward_timestamp_blocks(self):
        with self.assertRaisesRegex(ConsistencyHalted,"NOT_CONSECUTIVE"):
            await verify(read(1500),read(1000))

    async def test_excessive_gap_blocks(self):
        with self.assertRaisesRegex(ConsistencyHalted,"NOT_CONSECUTIVE"):
            await verify(read(1000),read(1500),max_separation_ms=400)

    async def test_invalid_gap_config(self):
        for value in (0,1501,True,1.5):
            with self.assertRaises(ValueError):
                await verify(read(1000),read(1500),max_separation_ms=value)

    async def test_bad_second_read_blocks(self):
        with self.assertRaisesRegex(ConsistencyHalted,"DOUBLE_READ_UNVERIFIED"):
            await verify(read(1000),None)

    async def test_unserializable_row_blocks(self):
        p=pos()
        p["unexpected"]=object()
        with self.assertRaisesRegex(ConsistencyHalted,"DOUBLE_READ_UNVERIFIED"):
            await verify(read(1000,[p]),read(1500,[p]))

    async def test_nonfinite_json_number_blocks(self):
        p=pos()
        p["extra"]=float("nan")
        with self.assertRaisesRegex(ConsistencyHalted,"DOUBLE_READ_UNVERIFIED"):
            await verify(read(1000,[p]),read(1500,[p]))



class RawConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_identical_raw_reads_pass_without_completeness_claim(self):
        reader = Reader(
            read(1000, [pos()], [], positions_complete=False, stops_complete=False),
            read(1500, [pos()], [], positions_complete=False, stops_complete=False),
        )
        r = await double_read_raw_inventory(reader)
        self.assertEqual(r.mode, "READ_ONLY_DISCOVERY")
        self.assertEqual((r.positions, r.stops), (1, 0))

    async def test_raw_value_drift_blocks(self):
        reader = Reader(
            read(1000, [pos("2")], []),
            read(1500, [pos("3")], []),
        )
        with self.assertRaisesRegex(ConsistencyHalted, "RAW_INVENTORY_CHANGED"):
            await double_read_raw_inventory(reader)

    async def test_raw_row_order_does_not_matter(self):
        rows = [pos(), dict(symbol="OTHER_USDT", positionId="2",
                            positionType=1, holdVol="1", state=1)]
        reader = Reader(read(1000, rows, []),
                        read(1500, list(reversed(rows)), []))
        r = await double_read_raw_inventory(reader)
        self.assertEqual(r.positions, 2)

    async def test_raw_excessive_gap_blocks(self):
        reader = Reader(read(1000), read(4001))
        with self.assertRaisesRegex(ConsistencyHalted, "RAW_INVENTORY_READS_NOT_CONSECUTIVE"):
            await double_read_raw_inventory(reader, max_separation_ms=3000)

    async def test_raw_bad_type_blocks(self):
        reader = Reader(read(1000), None)
        with self.assertRaisesRegex(ConsistencyHalted, "RAW_INVENTORY_DOUBLE_READ_UNVERIFIED"):
            await double_read_raw_inventory(reader)

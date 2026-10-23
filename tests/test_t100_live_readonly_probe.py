"""Strict GET-only venue probe tests, with a fake transport."""
import unittest
from app.t100_live_readonly_probe import ReadOnlyMexcProbe, VenueReadHalted


class FakeClient:
    def __init__(self):
        self.responses = []
        self.calls = []
        self.fail = False
    async def _private_get(self, path, params=None):
        self.calls.append((path, params))
        if self.fail:
            raise TimeoutError()
        return self.responses.pop(0)


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FakeClient()
        self.probe = ReadOnlyMexcProbe(self.client)

    async def test_flat_positions_is_list_not_inferred_from_null(self):
        self.client.responses = [[]]
        self.assertEqual(await self.probe.raw_open_positions(), [])

    async def test_position_payload_preserved(self):
        payload = [dict(positionId="10", symbol="SIREN_USDT")]
        self.client.responses = [payload]
        self.assertEqual(await self.probe.raw_open_positions(), payload)

    async def test_null_positions_fail(self):
        self.client.responses = [None]
        with self.assertRaisesRegex(VenueReadHalted, "POSITIONS_RESPONSE"):
            await self.probe.raw_open_positions()

    async def test_malformed_row_fails(self):
        self.client.responses = [[dict(positionId="10"), None]]
        with self.assertRaisesRegex(VenueReadHalted, "POSITIONS_RESPONSE"):
            await self.probe.raw_open_positions()

    async def test_positions_transport_failure(self):
        self.client.fail = True
        with self.assertRaisesRegex(VenueReadHalted, "POSITIONS_GET_FAILED"):
            await self.probe.raw_open_positions()

    async def test_stop_page_passes_raw_contract(self):
        payload = dict(page_num=2, page_size=100, total=101,
                       resultList=[dict(id="100")])
        self.client.responses = [payload]
        self.assertEqual(await self.probe.raw_stop_page(2,100), payload)
        self.assertEqual(self.client.calls[0][1]["page_num"], 2)

    async def test_stop_list_shape_not_guessed(self):
        self.client.responses = [[dict(id="100")]]
        with self.assertRaisesRegex(VenueReadHalted, "STOP_PAGE_RESPONSE"):
            await self.probe.raw_stop_page(1,100)

    async def test_stop_page_transport_failure(self):
        self.client.fail = True
        with self.assertRaisesRegex(VenueReadHalted, "STOP_PAGE_GET_FAILED"):
            await self.probe.raw_stop_page(1,100)

    async def test_invalid_stop_page_args(self):
        for page, size in ((0,100),(1,0),(1,101),(True,100),(1,True)):
            with self.assertRaisesRegex(VenueReadHalted, "INVALID_STOP_PAGE"):
                await self.probe.raw_stop_page(page,size)
        self.assertEqual(self.client.calls, [])

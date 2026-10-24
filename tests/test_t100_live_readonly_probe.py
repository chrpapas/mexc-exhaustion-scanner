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

    async def test_open_stops_empty_preserved(self):
        self.client.responses = [[]]
        self.assertEqual(await self.probe.raw_open_stops(), [])

    async def test_open_stops_malformed_row_blocks(self):
        self.client.responses = [[dict(id="99"), None]]
        with self.assertRaisesRegex(VenueReadHalted, "OPEN_STOPS_RESPONSE"):
            await self.probe.raw_open_stops()

    async def test_open_stops_transport_error_blocks(self):
        self.client.fail = True
        with self.assertRaisesRegex(VenueReadHalted, "OPEN_STOPS_GET_FAILED"):
            await self.probe.raw_open_stops()

    async def test_stop_page_preserves_documented_list_shape(self):
        payload = [dict(id="100"), dict(id="101")]
        self.client.responses = [payload]
        self.assertEqual(await self.probe.raw_stop_page(2,100), payload)
        self.assertEqual(self.client.calls[0][1]["page_num"], 2)
        self.assertEqual(self.client.calls[0][1]["is_finished"], 1)

    async def test_unfinished_stop_page_is_explicit(self):
        self.client.responses = [[]]
        self.assertEqual(await self.probe.raw_stop_page(1,20,is_finished=0), [])
        self.assertEqual(self.client.calls[0][1]["is_finished"], 0)

    async def test_stop_page_dict_shape_rejected(self):
        self.client.responses = [dict(page_num=1, resultList=[])]
        with self.assertRaisesRegex(VenueReadHalted, "STOP_PAGE_RESPONSE"):
            await self.probe.raw_stop_page(1,100)

    async def test_stop_page_malformed_row_rejected(self):
        self.client.responses = [[dict(id="100"), None]]
        with self.assertRaisesRegex(VenueReadHalted, "STOP_PAGE_RESPONSE"):
            await self.probe.raw_stop_page(1,100)

    async def test_stop_page_overflow_rejected(self):
        self.client.responses = [[dict(id=str(i)) for i in range(3)]]
        with self.assertRaisesRegex(VenueReadHalted, "STOP_PAGE_OVERFLOW"):
            await self.probe.raw_stop_page(1,2)

    async def test_stop_page_transport_failure(self):
        self.client.fail = True
        with self.assertRaisesRegex(VenueReadHalted, "STOP_PAGE_GET_FAILED"):
            await self.probe.raw_stop_page(1,100)

    async def test_invalid_stop_page_args(self):
        for page, size in ((0,100),(1,0),(1,101),(True,100),(1,True)):
            with self.assertRaisesRegex(VenueReadHalted, "INVALID_STOP_PAGE"):
                await self.probe.raw_stop_page(page,size)
        for finished in (-1,2,True,None):
            with self.assertRaisesRegex(VenueReadHalted, "INVALID_STOP_PAGE"):
                await self.probe.raw_stop_page(1,20,is_finished=finished)
        self.assertEqual(self.client.calls, [])


    async def test_funding_page_preserves_documented_dict_shape(self):
        payload = {
            "pageSize": 100,
            "totalCount": 1,
            "totalPage": 1,
            "currentPage": 1,
            "resultList": [{"id": 123}],
        }
        self.client.responses = [payload]
        self.assertEqual(
            await self.probe.raw_funding_page("10", page=1, size=100),
            payload,
        )
        self.assertEqual(
            self.client.calls[0],
            (
                "/api/v1/private/position/funding_records",
                {"position_id": 10, "page_num": 1, "page_size": 100},
            ),
        )

    async def test_funding_page_rejects_non_dict(self):
        self.client.responses = [[]]
        with self.assertRaisesRegex(VenueReadHalted, "FUNDING_PAGE_RESPONSE"):
            await self.probe.raw_funding_page("10", page=1, size=100)

    async def test_funding_page_rejects_malformed_rows(self):
        self.client.responses = [{
            "pageSize": 100, "totalCount": 1, "totalPage": 1,
            "currentPage": 1, "resultList": [None],
        }]
        with self.assertRaisesRegex(VenueReadHalted, "FUNDING_PAGE_ROWS"):
            await self.probe.raw_funding_page("10", page=1, size=100)

    async def test_funding_page_transport_failure(self):
        self.client.fail = True
        with self.assertRaisesRegex(VenueReadHalted, "FUNDING_PAGE_GET_FAILED"):
            await self.probe.raw_funding_page("10", page=1, size=100)

    async def test_invalid_funding_page_args(self):
        for pid, page, size in (
            ("", 1, 100), ("abc", 1, 100), ("0", 1, 100),
            ("10", 0, 100), ("10", 1, 0), ("10", 1, 101),
            ("10", True, 100), ("10", 1, True),
        ):
            with self.assertRaisesRegex(VenueReadHalted, "INVALID_FUNDING_PAGE"):
                await self.probe.raw_funding_page(pid, page=page, size=size)

"""Pure fake-transport tests for strict read-only pagination."""
import unittest
from app.t100_live_pagination import collect_verified_pages, PaginationHalted


class FakePages:
    def __init__(self, total, *, fail_page=None, changes=None):
        self.total = total
        self.fail_page = fail_page
        self.changes = changes or {}
        self.calls = []
    async def read_page(self, page, size):
        self.calls.append(page)
        if self.fail_page == page:
            raise TimeoutError()
        rows = [{"id": str(i)} for i in range((page-1)*size, min(page*size, self.total))]
        payload = dict(page_num=page, page_size=size, total=self.total, resultList=rows)
        payload.update(self.changes.get(page, {}))
        return payload


async def run(fake, **kw):
    return await collect_verified_pages(fake, page_size=2, identity=lambda r: r["id"], **kw)


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_is_explicitly_complete(self):
        r = await run(FakePages(0))
        self.assertEqual((r.total, r.page_count, len(r.rows)), (0,1,0))

    async def test_single_page(self):
        r = await run(FakePages(2))
        self.assertEqual(len(r.rows),2)

    async def test_multiple_pages(self):
        f=FakePages(5)
        r=await run(f)
        self.assertEqual((r.total,r.page_count), (5,3))
        self.assertEqual(f.calls,[1,2,3])

    async def test_changed_total_fails(self):
        with self.assertRaisesRegex(PaginationHalted,"TOTAL_CHANGED"):
            await run(FakePages(3,changes={2:{"total":4}}))

    async def test_duplicate_row_fails(self):
        with self.assertRaisesRegex(PaginationHalted,"DUPLICATE_ROW"):
            await run(FakePages(3,changes={2:{"resultList":[{"id":"0"}]}}))

    async def test_missing_row_id_fails(self):
        with self.assertRaisesRegex(PaginationHalted,"ROW_IDENTITY_INVALID"):
            await run(FakePages(1,changes={1:{"resultList":[{}]}}))

    async def test_missing_total_fails(self):
        with self.assertRaisesRegex(PaginationHalted,"INVALID_REPORTED_TOTAL"):
            await run(FakePages(1,changes={1:{"total":None}}))

    async def test_page_number_drift_fails(self):
        with self.assertRaisesRegex(PaginationHalted,"PAGE_METADATA_MISMATCH"):
            await run(FakePages(3,changes={2:{"page_num":3}}))

    async def test_page_size_drift_fails(self):
        with self.assertRaisesRegex(PaginationHalted,"PAGE_METADATA_MISMATCH"):
            await run(FakePages(3,changes={2:{"page_size":3}}))

    async def test_oversized_page_fails(self):
        with self.assertRaisesRegex(PaginationHalted,"PAGE_OVERFLOW"):
            await run(FakePages(2,changes={1:{"resultList":[{"id":"0"},{"id":"1"},{"id":"2"}]}}))

    async def test_short_page_before_total_fails(self):
        with self.assertRaisesRegex(PaginationHalted,"INCOMPLETE_PAGE"):
            await run(FakePages(4,changes={1:{"resultList":[{"id":"0"}]}}))

    async def test_transport_failure_fails(self):
        with self.assertRaisesRegex(PaginationHalted,"PAGE_READ_FAILED"):
            await run(FakePages(3,fail_page=2))

    async def test_max_pages_fails(self):
        with self.assertRaisesRegex(PaginationHalted,"PAGINATION_LIMIT_EXCEEDED"):
            await run(FakePages(5),max_pages=2)

    async def test_malformed_row_fails(self):
        with self.assertRaisesRegex(PaginationHalted,"PAGE_ROWS_MALFORMED"):
            await run(FakePages(1,changes={1:{"resultList":[None]}}))

    async def test_missing_identity_function_fails(self):
        with self.assertRaisesRegex(PaginationHalted,"ROW_IDENTITY_REQUIRED"):
            await collect_verified_pages(FakePages(0),page_size=2)

    async def test_invalid_bounds_fail(self):
        with self.assertRaisesRegex(PaginationHalted,"UNSAFE_PAGINATION_BOUNDS"):
            await run(FakePages(0),max_pages=101)

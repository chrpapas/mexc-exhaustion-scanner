"""Offline leader lease/fencing behavior tests."""
import unittest

from app.t100_live_leader_fencing import (
    LeaderLeaseCoordinator,
    LeaderLeaseHalted,
)


class Store:
    def __init__(self):
        self.holder = None
        self.token = 0
        self.current = False

    async def acquire(self, name, holder, ttl):
        if self.current and self.holder != holder:
            return 0, False
        if self.holder != holder:
            self.token += 1
        self.holder = holder
        self.current = True
        return self.token, True

    async def renew(self, name, holder, token, ttl):
        return (
            self.current
            and self.holder == holder
            and self.token == token
        )

    async def assert_current(self, name, holder, token):
        return (
            self.current
            and self.holder == holder
            and self.token == token
        )

    async def release(self, name, holder, token):
        if not await self.assert_current(name, holder, token):
            return False
        self.current = False
        return True

    def expire(self):
        self.current = False


class LeaderFencingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store()
        self.c = LeaderLeaseCoordinator(self.store)

    async def test_single_holder_acquires_and_renews_same_token(self):
        a = await self.c.acquire("render-instance-a")
        self.assertEqual(a.fencing_token, 1)
        b = await self.c.renew(a)
        self.assertEqual(b.fencing_token, 1)
        await self.c.require_current(a)
        self.assertFalse(a.live_authorization)

    async def test_second_holder_cannot_acquire_live_lease(self):
        await self.c.acquire("a")
        with self.assertRaisesRegex(
            LeaderLeaseHalted, "NOT_ACQUIRED"
        ):
            await self.c.acquire("b")

    async def test_takeover_after_expiry_increments_fencing_token(self):
        first = await self.c.acquire("a")
        self.store.expire()
        second = await self.c.acquire("b")
        self.assertEqual(first.fencing_token, 1)
        self.assertEqual(second.fencing_token, 2)
        with self.assertRaisesRegex(
            LeaderLeaseHalted, "FENCING_TOKEN_STALE"
        ):
            await self.c.require_current(first)
        await self.c.require_current(second)

    async def test_old_holder_cannot_renew_after_takeover(self):
        first = await self.c.acquire("a")
        self.store.expire()
        await self.c.acquire("b")
        with self.assertRaisesRegex(
            LeaderLeaseHalted, "LEASE_LOST"
        ):
            await self.c.renew(first)

    async def test_release_only_current_holder(self):
        first = await self.c.acquire("a")
        await self.c.release(first)
        second = await self.c.acquire("b")
        self.assertGreater(second.fencing_token, first.fencing_token)

    def test_ttl_bounds(self):
        for ttl in (0, 14, 121):
            with self.subTest(ttl=ttl):
                with self.assertRaises(ValueError):
                    LeaderLeaseCoordinator(Store(), ttl_seconds=ttl)


if __name__ == "__main__":
    unittest.main()

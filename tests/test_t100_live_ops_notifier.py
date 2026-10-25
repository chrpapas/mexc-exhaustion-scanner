"""Offline tests for T100 Discord operations notifier."""
import unittest

from app.t100_live_ops_notifier import OpsNotice, T100LiveOpsNotifier


class FakeTraderNotifier:
    def __init__(self):
        self.webhook_url = "https://example.invalid"
        self.calls = []
    async def close(self):
        pass
    async def send(self, title, description, fields=None, *, color=None):
        self.calls.append((title, description, fields, color))
        return True


class OpsNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_deduplicates_noncritical_notice(self):
        now = [100.0]
        notifier = T100LiveOpsNotifier(
            "https://example.invalid",
            cooldown_seconds=60,
            clock=lambda: now[0],
        )
        fake = FakeTraderNotifier()
        await notifier._notifier.close()
        notifier._notifier = fake

        notice = OpsNotice(
            key="health",
            title="Health",
            description="healthy",
            severity="success",
        )
        self.assertTrue(await notifier.send(notice))
        self.assertFalse(await notifier.send(notice))
        self.assertEqual(len(fake.calls), 1)
        now[0] += 61
        self.assertTrue(await notifier.send(notice))
        self.assertEqual(len(fake.calls), 2)

    async def test_force_bypasses_cooldown(self):
        notifier = T100LiveOpsNotifier(
            "https://example.invalid",
            cooldown_seconds=60,
            clock=lambda: 100.0,
        )
        fake = FakeTraderNotifier()
        await notifier._notifier.close()
        notifier._notifier = fake
        notice = OpsNotice(
            key="state",
            title="State",
            description="changed",
            severity="warning",
        )
        self.assertTrue(await notifier.send(notice))
        self.assertTrue(await notifier.send(notice, force=True))
        self.assertEqual(len(fake.calls), 2)

    async def test_invalid_notice_rejected(self):
        notifier = T100LiveOpsNotifier(None)
        with self.assertRaises(ValueError):
            await notifier.send(OpsNotice(
                key="x", title="x", description="x", severity="invalid"))
        with self.assertRaises(ValueError):
            await notifier.send(OpsNotice(
                key="", title="x", description="x", severity="info"))

    async def test_configured_reflects_webhook(self):
        self.assertFalse(T100LiveOpsNotifier(None).configured)
        n = T100LiveOpsNotifier("https://example.invalid")
        self.assertTrue(n.configured)
        await n.close()


if __name__ == "__main__":
    unittest.main()

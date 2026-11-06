"""Security regression tests for Discord webhook logging."""
from __future__ import annotations

import logging
import unittest

import httpx

from app.trader_notifier import TraderNotifier


class TraderNotifierSecurityTests(unittest.IsolatedAsyncioTestCase):
    def test_http_transport_loggers_are_not_info_enabled(self):
        self.assertGreaterEqual(
            logging.getLogger("httpx").getEffectiveLevel(), logging.WARNING
        )
        self.assertGreaterEqual(
            logging.getLogger("httpcore").getEffectiveLevel(), logging.WARNING
        )

    async def test_untrusted_http_error_body_cannot_leak_webhook_token(self):
        secret_url=(
            "https://discord.com/api/webhooks/123/"
            "SUPER_SECRET_WEBHOOK_TOKEN"
        )
        sensitive="THIS_TOKEN_MUST_NEVER_LEAK"
        async def handler(request: httpx.Request) -> httpx.Response:
            # An upstream failure could echo URL or secret payloads.
            return httpx.Response(
                403,request=request,
                text=f"Rejected {secret_url} {sensitive}",
            )
        notifier=TraderNotifier(secret_url)
        await notifier.client.aclose()
        notifier.client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),timeout=15.0,
        )
        with self.assertLogs("app.trader_notifier",level="ERROR") as captured:
            sent=await notifier.send("guardian incident","redacted")
        self.assertFalse(sent)
        logs="\n".join(captured.output)
        self.assertIn("403",logs)
        self.assertNotIn(secret_url,logs)
        self.assertNotIn("SUPER_SECRET_WEBHOOK_TOKEN",logs)
        self.assertNotIn(sensitive,logs)
        await notifier.close()

    async def test_failed_webhook_does_not_log_secret_url(self):
        secret_url = (
            "https://discord.com/api/webhooks/123/"
            "SUPER_SECRET_WEBHOOK_TOKEN"
        )

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, request=request, text="not found")

        notifier = TraderNotifier(secret_url)
        await notifier.client.aclose()
        notifier.client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=15.0,
        )

        with self.assertLogs("app.trader_notifier", level="ERROR") as captured:
            sent = await notifier.send("title", "description")

        self.assertFalse(sent)
        joined = "\n".join(captured.output)
        self.assertNotIn(secret_url, joined)
        self.assertNotIn("SUPER_SECRET_WEBHOOK_TOKEN", joined)
        self.assertIn("HTTPStatusError", joined)
        await notifier.close()


if __name__ == "__main__":
    unittest.main()

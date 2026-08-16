from __future__ import annotations

import asyncio
import os

from app.trader_notifier import TraderNotifier


async def main() -> None:
    url = os.getenv("DISCORD_TRADER_EVENTS_WEBHOOK_URL") or os.getenv("DISCORD_TRADER_WEBHOOK_URL")
    if not url:
        raise RuntimeError("DISCORD_TRADER_EVENTS_WEBHOOK_URL is required")
    notifier = TraderNotifier(url)
    try:
        ok = await notifier.send(
            "✅ TRADER DISCORD TEST",
            "The dedicated trader-events webhook is connected. Important trading/server events will be sent here.",
            [{"name": "Status", "value": "Webhook delivery successful", "inline": False}],
            color=0x2ECC71,
        )
        if not ok:
            raise RuntimeError("Discord webhook test was not sent")
        print("Trader Discord webhook test sent successfully.")
    finally:
        await notifier.close()


if __name__ == "__main__":
    asyncio.run(main())

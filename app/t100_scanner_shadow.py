from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import replace

from app.config import Settings
from app.worker import ScannerWorker, configure_logging

LOGGER = logging.getLogger(__name__)


class ResearchShadowWorker(ScannerWorker):
    async def check_trader_watchdog(self) -> None:
        # The promoted T100_200 service owns its own heartbeat/events. The legacy
        # scanner's watchdog targets the retired portfolio_short_trader heartbeat
        # and would otherwise emit false stale alerts after promotion.
        return


async def main() -> None:
    """Run the legacy scanner as a silent research/data collector.

    T100_200 has its own frozen Min30 P2 service. The legacy Min15 worker remains
    useful for historical/research tables, but it must not emit subscriber
    signals, old-strategy performance reports, or legacy-trader watchdog alerts.
    """
    base = Settings.from_env()
    settings = replace(
        base,
        discord_webhook_url=None,
        discord_performance_webhook_url=None,
    )
    configure_logging(settings.log_level)
    LOGGER.info(
        "Legacy scanner running in research-shadow mode; old subscriber/performance/watchdog outputs are muted"
    )
    worker = ResearchShadowWorker(settings)
    loop = asyncio.get_running_loop()
    for system_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(system_signal, worker.stop_event.set)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())

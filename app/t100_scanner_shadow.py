from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import replace

from app.config import Settings
from app.worker import ScannerWorker, configure_logging

LOGGER = logging.getLogger(__name__)


async def main() -> None:
    """Run the legacy scanner as a silent research/data collector.

    T100_200 has its own frozen Min30 P2 service. The legacy Min15 worker remains
    useful for the historical/research tables and watchdog, but it must not emit
    subscriber signals or the old strategy performance report after promotion.
    """
    base = Settings.from_env()
    settings = replace(
        base,
        discord_webhook_url=None,
        discord_performance_webhook_url=None,
    )
    configure_logging(settings.log_level)
    LOGGER.info(
        "Legacy scanner running in research-shadow mode; subscriber and performance webhooks are muted"
    )
    worker = ScannerWorker(settings)
    loop = asyncio.get_running_loop()
    for system_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(system_signal, worker.stop_event.set)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())

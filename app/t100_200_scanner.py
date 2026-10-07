from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import UTC, datetime, timedelta

from app.t100_200_strategy import STRATEGY_ID
from app.t100_200_worker import M30, T100Worker, _floor_30m, configure_logging

LOGGER = logging.getLogger(__name__)


class T100Scanner(T100Worker):
    """Signal producer only.

    This service owns the frozen Min30 universe scan, P2 lifecycle, Daily-Core /
    Persistence admission, physical episode staging, and t100_p2_signals writes.
    It never admits portfolio positions and never applies exits/funding.
    """

    async def process_eval(self, eval_at: datetime) -> None:
        rows = await self._build_eval_rows(eval_at)
        for row in rows:
            await self._process_symbol_row(row)

        await self.db.pool.execute(
            """
            UPDATE t100_runtime
            SET last_eval_at=$1,updated_at=now()
            WHERE singleton=true
            """,
            eval_at,
        )
        eligible = await self.db.pool.fetchval(
            """
            SELECT count(*) FROM t100_p2_signals
            WHERE p2_at=$1 AND eligible_stage2=true
            """,
            eval_at,
        )
        await self.db.heartbeat(
            "mexc-t100-200-scanner",
            {
                "strategy_id": STRATEGY_ID,
                "last_eval_at": eval_at.isoformat(),
                "evaluated_symbols": len(rows),
                "eligible_stage2_created": int(eligible or 0),
            },
        )
        LOGGER.info(
            "T100 scanner eval complete %s symbols=%d eligible_stage2=%d",
            eval_at.isoformat(),
            len(rows),
            int(eligible or 0),
        )

    async def cycle(self) -> None:
        latest_eval = _floor_30m(datetime.now(UTC))
        runtime = await self._runtime()
        last_eval = runtime.get("last_eval_at")

        if last_eval is not None and last_eval >= latest_eval:
            await self.db.heartbeat(
                "mexc-t100-200-scanner",
                {
                    "strategy_id": STRATEGY_ID,
                    "last_eval_at": last_eval.isoformat(),
                    "status": "waiting_for_next_min30_close",
                },
            )
            return

        await self.refresh_contracts()
        await self.sync_market_history()

        if last_eval is None:
            evals = [latest_eval]
        else:
            next_eval = last_eval + M30
            evals = []
            while next_eval <= latest_eval:
                evals.append(next_eval)
                next_eval += M30
            if len(evals) > 480:
                raise RuntimeError(
                    "T100 scanner catch-up exceeds 10 days; manual reconstruction required"
                )

        for eval_at in evals:
            await self.process_eval(eval_at)

    async def run(self) -> None:
        await self.initialize()
        LOGGER.info(
            "T100 scanner started strategy=%s role=signal_producer_only",
            STRATEGY_ID,
        )
        try:
            while not self.stop_event.is_set():
                started = asyncio.get_running_loop().time()
                try:
                    await self.cycle()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception("T100 scanner cycle failed")
                elapsed = asyncio.get_running_loop().time() - started
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(),
                        timeout=max(1.0, self.poll_seconds - elapsed),
                    )
                except TimeoutError:
                    pass
        finally:
            await self.close()


async def main() -> None:
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    worker = T100Scanner()
    loop = asyncio.get_running_loop()
    for system_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(system_signal, worker.stop_event.set)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())

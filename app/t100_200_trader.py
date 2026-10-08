from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import UTC, datetime

from app.t100_200_strategy import PAPER_RUN_ID, STRATEGY_ID
from app.t100_200_worker import M30, T100Worker, configure_logging

LOGGER = logging.getLogger(__name__)


class T100Trader(T100Worker):
    """Portfolio consumer only.

    The scanner is the sole signal producer. This service only:
      1) consumes eligible Stage2 rows already committed to t100_p2_signals,
      2) manages the four-slot paper portfolio,
      3) applies funding and frozen Min30 exits,
      4) emits trader events/heartbeat.

    It never refreshes the contract universe, calculates scanner features,
    evaluates Daily-Core/Persistence, or creates P2/stage rows.
    """

    async def initialize(self) -> None:
        await self.db.connect()
        await self.db.migrate()
        row = await self.db.pool.fetchrow(
            "SELECT * FROM t100_runtime WHERE singleton=true"
        )
        if row is None:
            raise RuntimeError(
                "T100 scanner runtime is missing; scanner must initialize before trader"
            )
        if row["strategy_id"] != STRATEGY_ID:
            raise RuntimeError("t100_runtime strategy id mismatch")

        # Migration from the short-lived combined scanner/trader process:
        # the combined worker had already processed portfolio bars through
        # last_eval_at, so seed the trader cursor there exactly once.
        if row["last_trader_eval_at"] is None:
            await self.db.pool.execute(
                """
                UPDATE t100_runtime
                SET last_trader_eval_at=last_eval_at,updated_at=now()
                WHERE singleton=true AND last_trader_eval_at IS NULL
                """
            )

        runtime = await self._runtime()
        cursor = runtime.get("last_trader_eval_at")
        if cursor is not None:
            await self._record_equity_snapshot(cursor)

    async def _record_equity_snapshot(self, snapshot_at: datetime) -> None:
        positions = await self._open_positions()
        runtime = await self._runtime()
        equity = await self._equity(positions)
        realized_equity = float(runtime["realized_equity_usdt"])
        unrealized = equity - realized_equity
        gross_notional = sum(float(p["notional_usdt"]) for p in positions)
        gross_exposure_pct = (
            gross_notional / equity * 100.0 if equity > 0 else None
        )
        await self.db.pool.execute(
            """
            INSERT INTO t100_equity_snapshots(
                snapshot_at,equity_usdt,realized_equity_usdt,unrealized_pnl_usdt,
                gross_notional_usdt,gross_exposure_pct,open_positions
            ) VALUES($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT(snapshot_at) DO UPDATE SET
                equity_usdt=EXCLUDED.equity_usdt,
                realized_equity_usdt=EXCLUDED.realized_equity_usdt,
                unrealized_pnl_usdt=EXCLUDED.unrealized_pnl_usdt,
                gross_notional_usdt=EXCLUDED.gross_notional_usdt,
                gross_exposure_pct=EXCLUDED.gross_exposure_pct,
                open_positions=EXCLUDED.open_positions
            """,
            snapshot_at,
            equity,
            realized_equity,
            unrealized,
            gross_notional,
            gross_exposure_pct,
            len(positions),
        )

    async def _write_heartbeat(self) -> None:
        runtime = await self._runtime()
        positions = await self._open_positions()
        await self.db.heartbeat(
            "mexc-t100-200-trader",
            {
                "strategy_id": STRATEGY_ID,
                "run_id": PAPER_RUN_ID,
                "mode": "paper",
                "scanner_last_eval_at": (
                    runtime["last_eval_at"].isoformat()
                    if runtime.get("last_eval_at") is not None
                    else None
                ),
                "trader_last_eval_at": (
                    runtime["last_trader_eval_at"].isoformat()
                    if runtime.get("last_trader_eval_at") is not None
                    else None
                ),
                "open_positions": len(positions),
                "equity_usdt": round(await self._equity(positions), 4),
            },
        )

    async def _consume_one_eval(self, eval_at: datetime) -> None:
        # Same frozen chronology as certification:
        # existing positions see this completed bar first; signals timestamped
        # at this completion are admitted only afterwards.
        await self._process_position_bar(eval_at)
        await self._admit_pending_stage2(eval_at)
        await self.db.pool.execute(
            """
            UPDATE t100_runtime
            SET last_trader_eval_at=$1,updated_at=now()
            WHERE singleton=true
            """,
            eval_at,
        )
        await self._record_equity_snapshot(eval_at)
        LOGGER.info("T100 trader consumed scanner eval %s", eval_at.isoformat())

    async def cycle(self) -> None:
        # Serialize across rolling Render deploy overlap just like the legacy
        # trader's signal-consumer lock.
        async with self.db.pool.acquire() as conn:
            acquired = await conn.fetchval(
                "SELECT pg_try_advisory_lock(hashtext('t100_200_trader_consumer'))"
            )
            if not acquired:
                LOGGER.debug("T100 trader consumer lease busy")
                return
            try:
                runtime = await self._runtime()
                scanner_eval = runtime.get("last_eval_at")
                trader_eval = runtime.get("last_trader_eval_at")

                if scanner_eval is None:
                    await self._write_heartbeat()
                    return

                # Crash recovery for a signal committed after the scanner cursor
                # but before a previous trader instance admitted it.
                if trader_eval is not None and trader_eval >= scanner_eval:
                    await self._admit_pending_stage2(scanner_eval)
                    await self._write_heartbeat()
                    return

                if trader_eval is None:
                    # initialize() normally seeds this. Defensive fail-closed.
                    await self.db.pool.execute(
                        """
                        UPDATE t100_runtime
                        SET last_trader_eval_at=$1,updated_at=now()
                        WHERE singleton=true
                        """,
                        scanner_eval,
                    )
                    await self._admit_pending_stage2(scanner_eval)
                    await self._write_heartbeat()
                    return

                next_eval = trader_eval + M30
                while next_eval <= scanner_eval:
                    await self._consume_one_eval(next_eval)
                    next_eval += M30

                await self._write_heartbeat()
            finally:
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtext('t100_200_trader_consumer'))"
                )

    async def run(self) -> None:
        await self.initialize()
        LOGGER.info(
            "T100 trader started strategy=%s role=db_signal_listener_only",
            STRATEGY_ID,
        )
        await self.notifier.send(
            "T100_225 PAPER TRADER STARTED",
            "Listening only to scanner-produced eligible Stage2 signals.",
            [
                {"name": "Strategy", "value": STRATEGY_ID, "inline": False},
                {"name": "Signal source", "value": "t100_p2_signals", "inline": True},
                {"name": "Scanner inside trader", "value": "NO", "inline": True},
            ],
        )
        try:
            while not self.stop_event.is_set():
                try:
                    await self.cycle()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception("T100 trader cycle failed")
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(),
                        timeout=max(1.0, self.poll_seconds),
                    )
                except TimeoutError:
                    pass
        finally:
            await self.close()


async def main() -> None:
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    worker = T100Trader()
    loop = asyncio.get_running_loop()
    for system_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(system_signal, worker.stop_event.set)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())

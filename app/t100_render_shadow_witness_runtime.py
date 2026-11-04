"""Explicit opt-in Render T100 shadow runtime with a durable PostgreSQL latch.

Not deployed and not activated by the existing Render start command.
Never enables MEXC orders. Uses already reviewed read-only shadow cycle
plus narrow Postgres heartbeat writer. Separate migration 031 and roles
required before configuring this as a Render worker start command.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import signal
import sys
import time

from app.t100_live_shadow_runtime import T100LiveShadowRuntime
from app.t100_live_ops_notifier import OpsNotice
from app.t100_render_shadow_witness import (
    RenderShadowWitness, ShadowWitnessFailure, connect_render_witness,
)

LOGGER = logging.getLogger(__name__)


class WitnessRuntimeHalted(RuntimeError):
    pass


def runtime_config(env):
    if (
        env.get("T100_EXECUTION_MODE") != "paper"
        or env.get("T100_LIVE_ORDER_API_ENABLED") != "false"
        or env.get("T100_LIVE_KILL_SWITCH") != "true"
        or env.get("T100_RENDER_SHADOW_WITNESS_ENABLED") != "true"
    ):
        raise WitnessRuntimeHalted("RENDER_SHADOW_PAPER_KILL_WITNESS_REQUIRED")
    scope = env.get("T100_RENDER_SHADOW_SCOPE_SHA256", "")
    if re.fullmatch(r"[a-f0-9]{64}", scope) is None:
        raise WitnessRuntimeHalted("RENDER_SHADOW_WITNESS_SCOPE_UNVERIFIED")
    dsn = env.get("T100_RENDER_SHADOW_WITNESS_DATABASE_URL", "")
    if not (dsn.startswith("postgresql://") or dsn.startswith("postgres://")):
        raise WitnessRuntimeHalted("RENDER_SHADOW_WITNESS_DSN_REQUIRED")
    return scope, dsn


async def run(*, environ=None, max_cycles: int | None = None,
              runtime_factory=T100LiveShadowRuntime,
              connect=connect_render_witness):
    """Run shadow and persist the one-way kill state before next cycle.

    Fail CLOSED on missing DB, malformed previous record, write failures.
    A new process restores a previously latched kill BEFORE any MEXC GET.
    """
    env = os.environ if environ is None else environ
    scope, dsn = runtime_config(env)
    if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
        raise WitnessRuntimeHalted("RENDER_SHADOW_INVALID_CYCLE_LIMIT")
    conn = await connect(dsn)
    runtime = None
    try:
        witness = RenderShadowWitness(conn, scope_sha256=scope)
        await witness.acquire_singleton()
        previous = await witness.restore()
        runtime = runtime_factory()
        # Preserve the historical first kill reason across process restarts.
        if previous is not None and previous.kill_latched:
            runtime._trip(previous.kill_reason)
            await runtime._notify_kill_once()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signum, runtime.stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass
        if previous is not None and previous.run_id == runtime._run_id:
            raise WitnessRuntimeHalted("RENDER_SHADOW_RUN_ID_REUSE")
        count = 0
        while not runtime.stop_event.is_set():
            started = time.monotonic()
            try:
                report = await runtime.cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Even an unhandled read exception must be represented in
                # durable state, not advertised as a healthy heartbeat.
                runtime._trip("READ_FAILURE")
                await witness.record(
                    run_id=runtime._run_id, cycle_index=count,
                    state="OBSERVING_KILL_LATCHED", kill_latched=True,
                    kill_reason=runtime.kill.first_reason,
                )
                raise WitnessRuntimeHalted("RENDER_SHADOW_OBSERVATION_FAILED") from None
            # This is the only path to a positive heartbeat. An uncommitted
            # cycle never counts; if DB fails, the worker exits nonzero.
            observed = await witness.record(
                run_id=runtime._run_id, cycle_index=count,
                state=report.status, kill_latched=report.kill_tripped,
                kill_reason=report.kill_reason,
            )
            if observed.kill_latched and not runtime.kill.tripped:
                runtime._trip(observed.kill_reason)
                await runtime._notify_kill_once()
            LOGGER.info(
                "T100 Render durable shadow revision=%s kill_latched=%s "
                "entry_allowed=false live_authorization=false",
                observed.revision, observed.kill_latched,
            )
            count += 1
            if max_cycles is not None and count >= max_cycles:
                return observed
            elapsed = time.monotonic() - started
            remaining = max(1.0, runtime.config.cycle_seconds - elapsed)
            try:
                await asyncio.wait_for(runtime.stop_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass
        return None
    finally:
        if runtime is not None:
            await runtime.close()
        await conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="T100 Render shadow with persistent nonauthorizing kill witness"
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        result = asyncio.run(run(max_cycles=1 if args.once else None))
        print("T100_RENDER_SHADOW_WITNESS=" +
              ("NO_COMPLETED_CYCLE" if result is None else "OBSERVATION_COMMITTED_NONAUTHORIZING"))
        print("MEXC_ORDERS=ZERO LIVE_AUTHORIZATION=FALSE")
    except Exception as exc:
        print("T100_RENDER_SHADOW_WITNESS_HALTED=" +
              (str(exc) if isinstance(exc, (WitnessRuntimeHalted, ShadowWitnessFailure))
               else "RUNTIME_OR_DURABILITY_FAILED"))
        print("MEXC_ORDERS=ZERO LIVE_AUTHORIZATION=FALSE")
        sys.exit(2)


if __name__ == "__main__":
    main()

"""Real-source, GET/SELECT-only wiring for the V40 four-slot supervisor.

This is NOT a live executor. Connects production Stage2, account-wide MEXC
inventory, the durable four-slot ledger, and the independent guardian gate.
Only a fully FLAT snapshot can be handed to V40PortfolioSupervisor; a nonflat
portfolio requires independently verified per-position completed-bar and
native-exit projections that are not yet released.

The ports have NO exchange POST, stop mutation, order, journal or DB write
method. There is no LiveMutationPermit issuance. Errors halt closed.
Never interpret this worker as four-slot order execution or a latch reset.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from typing import Any, Mapping


FLAG = "T100_V40_FOUR_SLOT_READONLY_WIRING_ENABLED"
SOURCE_DSN = "T100_V40_SIGNAL_READER_DSN"
LEDGER_DSN = "T100_V40_EXECUTOR_RUNTIME_DSN"


@dataclass(frozen=True)
class V40FourSlotWiringReport:
    status: str
    observed_slots: int | None = None
    candidate_count: int | None = None
    guardian_status: str = "NOT_OBSERVED"
    source_status: str = "NOT_OBSERVED"
    scanner_connected: bool = False
    account_snapshot_connected: bool = False
    guardian_connected: bool = False
    complete_exit_runtime_connected: bool = False
    exchange_posts: int = 0
    database_writes: int = 0
    entry_allowed: bool = False
    live_authorization: bool = False


def validate_readonly_config(env: Mapping[str, str]) -> bool:
    """Hard paper/kill controls checked BEFORE importing network clients."""
    if not isinstance(env, Mapping):
        return False
    if not all((
        env.get(FLAG) == "true",
        env.get("T100_V40_EXECUTOR_DISABLED_WORKER") == "true",
        env.get("T100_EXECUTION_MODE") == "paper",
        env.get("TRADING_MODE") == "paper",
        env.get("T100_LIVE_KILL_SWITCH") == "true",
        env.get("T100_LIVE_ORDER_API_ENABLED") == "false",
        env.get("EXECUTION_ENABLED") == "false",
        env.get("T100_V40_ACCOUNT_PROFILE") == "DEDICATED_WEB_PILOT",
        not env.get("DATABASE_URL"),
        not env.get("T100_LIVE_DATABASE_URL"),
        not env.get("T100_V40_TRADER_API_KEY"),
        not env.get("T100_V40_TRADER_API_SECRET"),
        not env.get("T100_V40_FIRST_CANARY_ORDER_WORKER_ENABLED") == "true",
    )):
        return False
    source, ledger = env.get(SOURCE_DSN), env.get(LEDGER_DSN)
    if (
        type(source) is not str or type(ledger) is not str
        or source == ledger
        or any(
            not s.startswith(("postgresql://", "postgres://")) or len(s) > 3000
            for s in (source, ledger)
        )
    ):
        return False
    key, secret = env.get("MEXC_API_KEY"), env.get("MEXC_API_SECRET")
    return (
        type(key) is str and type(secret) is str
        and len(key) >= 12 and key == key.strip()
        and len(secret) >= 16 and secret == secret.strip()
    )


class V40ReadOnlyPortfolioPorts:
    """No mutation methods: the supervisor must stop before invoking them."""

    def __init__(self, frame: Any) -> None:
        self._frame = frame
        self.mutation_calls = 0

    async def observe(self) -> Any:
        return self._frame

    async def enter_one(self, **kwargs: Any) -> Any:
        self.mutation_calls += 1
        raise RuntimeError("V40_PORTFOLIO_READONLY_ENTRY_FORBIDDEN")

    async def tighten_one_stop(self, *args: Any, **kwargs: Any) -> Any:
        self.mutation_calls += 1
        raise RuntimeError("V40_PORTFOLIO_READONLY_STOP_FORBIDDEN")

    async def reconcile_native_close(self, *args: Any, **kwargs: Any) -> Any:
        self.mutation_calls += 1
        raise RuntimeError("V40_PORTFOLIO_READONLY_CLOSE_FORBIDDEN")

    async def persist_unchanged_stop_checkpoint(
        self, *args: Any, **kwargs: Any,
    ) -> Any:
        self.mutation_calls += 1
        raise RuntimeError("V40_PORTFOLIO_READONLY_CURSOR_WRITE_FORBIDDEN")


async def examine_wired_readonly(
    *, source: Any, ledger: Any, signed_get: Any, guardian: Any,
    nonflat_guardian: Any, observed_at: datetime,
) -> V40FourSlotWiringReport:
    """Actual adapter composition, but never calls a live mutation port."""
    from app.t100_live_four_slot_mexc_readonly import _components
    from app.t100_live_four_slot_snapshot_coordinator import (
        FourSlotSnapshotCoordinator,
    )
    from app.t100_v40_live_portfolio_supervisor import (
        V40PortfolioCycleFrame, V40PortfolioSupervisor,
    )
    from app.t100_live_execution import LiveCaps

    try:
        venue, completeness = _components(signed_get)
        snap = await FourSlotSnapshotCoordinator(
            ledger=ledger, venue=venue, completeness=completeness,
        ).inspect()
    except Exception:
        return V40FourSlotWiringReport("V40_FOUR_SLOT_SNAPSHOT_GET_OR_SQL_HALT")

    if snap.status not in (
        "FLAT_OBSERVED_NONAUTHORIZING",
        "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
    ):
        return V40FourSlotWiringReport(
            "V40_FOUR_SLOT_SNAPSHOT_UNVERIFIED_HALT",
            observed_slots=snap.occupied_slots,
        )
    if snap.status == "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING":
        try:
            nonflat = await nonflat_guardian.verify()
        except Exception:
            return V40FourSlotWiringReport(
                "V40_FOUR_SLOT_NONFLAT_GUARDIAN_GET_OR_SQL_HALT",
                observed_slots=snap.occupied_slots,
                account_snapshot_connected=True,
            )
        # Never synthesize a "no bar due" statement or assume that a native
        # stop/touch has completed. The live exit handoff is still unreleased.
        return V40FourSlotWiringReport(
            "V40_FOUR_SLOT_NONFLAT_BAR_EXIT_RUNTIME_NOT_CONNECTED_HALT",
            observed_slots=snap.occupied_slots,
            guardian_status=nonflat.status,
            account_snapshot_connected=True,
            guardian_connected=nonflat.guardian_protected_observation_fresh is True,
        )
    if snap.occupied_slots != 0:
        return V40FourSlotWiringReport("V40_FOUR_SLOT_FLAT_COUNT_INVALID_HALT")

    # Every pass re-reads the guardian's current immutable scope and all
    # critical/pending alerts. A historically flat account is NOT sufficient.
    try:
        gate = await guardian.verify()
        batch = await source.latest_candidates(observed_at=observed_at)
    except Exception:
        return V40FourSlotWiringReport(
            "V40_FOUR_SLOT_GUARDIAN_OR_SOURCE_READ_HALT",
            observed_slots=0, account_snapshot_connected=True,
        )

    frame = V40PortfolioCycleFrame(
        snapshot=snap, guardian=gate, source=batch, completed_exit=None,
        no_bar_due_ids=(),
    )
    ports = V40ReadOnlyPortfolioPorts(frame)
    # Read-only constructor placeholders; NO permit can be issued, so these
    # are not live risk caps, sizing or approval for the historical strategy.
    read_only_placeholder_caps = LiveCaps(
        max_order_usdt=Decimal("1"),
        max_portfolio_usdt=Decimal("1"),
        max_daily_loss_usdt=Decimal("1"),
    )
    supervisor = V40PortfolioSupervisor(
        ports, caps=read_only_placeholder_caps,
        parity_report={}, holder_id="v40-four-slot-GET-only",
    )
    result = await supervisor.run_one(permit=None)
    if ports.mutation_calls != 0:
        return V40FourSlotWiringReport(
            "V40_FOUR_SLOT_READONLY_MUTATION_BOUNDARY_BROKEN",
        )
    return V40FourSlotWiringReport(
        status=result.status,
        observed_slots=0,
        candidate_count=len(batch.candidates),
        guardian_status=gate.status,
        source_status=batch.status,
        scanner_connected=batch.history_complete is True
            and batch.read_only_repeatable_read is True,
        account_snapshot_connected=True,
        guardian_connected=gate.critical_latch_absent is True
            and gate.guardian_fresh is True and gate.outbox_verified is True,
    )


async def run_once(env: Mapping[str, str]) -> V40FourSlotWiringReport:
    if not validate_readonly_config(env):
        return V40FourSlotWiringReport("V40_FOUR_SLOT_READONLY_CONFIG_HALT")

    # Network dependencies are imported only after the hard paper/kill gate.
    import asyncpg
    import httpx
    from app.t100_live_guardian_readonly_once import GetOnlyMexcTransport
    from app.t100_live_v40_stage2_source import PostgresV40Stage2Source
    from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
    from app.t100_v40_guardian_post_gate import PostgresV40GuardianPostGate
    from app.t100_v40_unreleased_nonflat_guardian_evidence import (
        PostgresV40NonflatGuardianEvidence,
    )

    source_pool = ledger_pool = None
    try:
        source_pool = await asyncpg.create_pool(
            dsn=env[SOURCE_DSN], ssl="require", min_size=1, max_size=1,
            timeout=9, command_timeout=12,
            server_settings={"default_transaction_read_only": "on"},
        )
        ledger_pool = await asyncpg.create_pool(
            dsn=env[LEDGER_DSN], ssl="require", min_size=1, max_size=1,
            timeout=9, command_timeout=12,
            server_settings={"default_transaction_read_only": "on"},
        )
        async with httpx.AsyncClient(
            base_url=GetOnlyMexcTransport.BASE_URL,
            trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(8.0),
        ) as client:
            get_only = GetOnlyMexcTransport(
                client,
                api_key=env["MEXC_API_KEY"],
                api_secret=env["MEXC_API_SECRET"],
            )
            return await asyncio.wait_for(
                examine_wired_readonly(
                    source=PostgresV40Stage2Source(
                        source_pool, restricted_source_reader=True,
                    ),
                    ledger=PostgresFourSlotLedgerReader(ledger_pool),
                    signed_get=get_only,
                    guardian=PostgresV40GuardianPostGate(ledger_pool),
                    nonflat_guardian=PostgresV40NonflatGuardianEvidence(
                        ledger_pool,
                    ),
                    observed_at=datetime.now(timezone.utc),
                ),
                timeout=55,
            )
    except Exception:
        # No exception text, DSN, URL, account data or token in Render logs.
        return V40FourSlotWiringReport(
            "V40_FOUR_SLOT_READONLY_CONNECTION_OR_CONTEXT_HALT",
        )
    finally:
        if source_pool is not None:
            await source_pool.close()
        if ledger_pool is not None:
            await ledger_pool.close()


async def run_readonly_monitor(
    env: Mapping[str, str], *, max_cycles: int | None = None,
    interval_seconds: int = 120,
) -> int:
    if (
        type(interval_seconds) is not int or interval_seconds < 60
        or max_cycles is not None
        and (type(max_cycles) is not int or not 1 <= max_cycles <= 100000)
    ):
        return 2
    cycles=0
    while True:
        report = await run_once(env)
        print(json.dumps(asdict(report), sort_keys=True), flush=True)
        cycles+=1
        # A latch/unverified venue/SQL is a TERMINAL halt, not an automatic
        # retry loop. Read-only monitoring never clears the incident.
        if report.status not in (
            "V40_SUPERVISOR_WAIT_GENUINE_STAGE2",
            "V40_SUPERVISOR_ENTRY_PERMIT_UNAVAILABLE",
            "V40_SUPERVISOR_DUPLICATE_SYMBOL_SKIP",
            "V40_SUPERVISOR_SAME_BAR_PRIORITY_UNVERIFIED",
        ):
            return 2
        if max_cycles is not None and cycles >= max_cycles:
            return 0
        await asyncio.sleep(float(interval_seconds))


def main() -> int:
    # Direct invocation is always one shot, never an order worker.
    return asyncio.run(run_readonly_monitor(dict(os.environ), max_cycles=1))


if __name__ == "__main__":
    raise SystemExit(main())

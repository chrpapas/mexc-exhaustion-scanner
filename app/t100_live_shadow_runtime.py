"""Continuous Render-safe T100 live-shadow supervisor.

This process is observation-only:
- signed GET-only MEXC preflight reads
- bounded private WebSocket observation
- safety classification / one-way kill latch
- Discord operational notifications

It never imports or invokes the live order gateway, stop mutation code, or DB
checkpoint writer. A healthy cycle is evidence only and never authorizes entry.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
import logging
import os
import signal
import time
from typing import Any, Awaitable, Callable

from app.t100_live_continuity_probe import run_continuity_probe
from app.t100_live_guards import T100ExecutionControls
from app.t100_live_kill import OperationalKillLatch
from app.t100_live_ops_notifier import OpsNotice, T100LiveOpsNotifier
from app.t100_live_preflight import read_only_preflight


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShadowRuntimeConfig:
    cycle_seconds: float = 90.0
    ws_window_seconds: float = 30.0
    health_report_seconds: float = 21600.0
    notification_cooldown_seconds: float = 900.0
    require_discord: bool = True

    @classmethod
    def from_env(cls) -> "ShadowRuntimeConfig":
        def number(name: str, default: str) -> float:
            raw = os.getenv(name, default)
            try:
                value = float(raw)
            except ValueError as exc:
                raise ValueError(f"{name}_INVALID") from exc
            if not value > 0:
                raise ValueError(f"{name}_INVALID")
            return value

        raw_require = os.getenv("T100_SHADOW_REQUIRE_DISCORD", "true")
        normalized = raw_require.strip().lower()
        if normalized not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise ValueError("T100_SHADOW_REQUIRE_DISCORD_INVALID")
        require_discord = normalized in {"true", "1", "yes", "on"}

        config = cls(
            cycle_seconds=number("T100_SHADOW_CYCLE_SECONDS", "90"),
            ws_window_seconds=number("T100_SHADOW_WS_WINDOW_SECONDS", "30"),
            health_report_seconds=number(
                "T100_SHADOW_HEALTH_REPORT_SECONDS", "21600"
            ),
            notification_cooldown_seconds=number(
                "T100_SHADOW_NOTIFICATION_COOLDOWN_SECONDS", "900"
            ),
            require_discord=require_discord,
        )
        if not 15 <= config.cycle_seconds <= 3600:
            raise ValueError("T100_SHADOW_CYCLE_SECONDS_OUT_OF_RANGE")
        if not 5 <= config.ws_window_seconds <= 120:
            raise ValueError("T100_SHADOW_WS_WINDOW_SECONDS_OUT_OF_RANGE")
        if config.ws_window_seconds >= config.cycle_seconds:
            raise ValueError("T100_SHADOW_WS_WINDOW_MUST_BE_BELOW_CYCLE")
        if not 300 <= config.health_report_seconds <= 86400:
            raise ValueError("T100_SHADOW_HEALTH_REPORT_SECONDS_OUT_OF_RANGE")
        if not 30 <= config.notification_cooldown_seconds <= 86400:
            raise ValueError(
                "T100_SHADOW_NOTIFICATION_COOLDOWN_SECONDS_OUT_OF_RANGE"
            )
        return config


@dataclass(frozen=True)
class ShadowCycleReport:
    status: str
    positions: int
    current_tpsl: int
    protection_status: str
    critical_inventory_consistent: bool
    symbol_redundancy_verified: bool
    margin_witness_observed: bool
    continuity_status: str
    ws_private_events: int
    kill_tripped: bool
    kill_reason: str | None
    account_wide_completeness_verified: bool = False
    exchange_atomicity_verified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False


class T100LiveShadowRuntime:
    def __init__(
        self,
        *,
        config: ShadowRuntimeConfig | None = None,
        notifier: T100LiveOpsNotifier | None = None,
        preflight_runner: Callable[..., Awaitable[dict[str, Any]]] = read_only_preflight,
        continuity_runner: Callable[..., Awaitable[dict[str, Any]]] = run_continuity_probe,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        controls = T100ExecutionControls.from_env()
        if controls.mode != "paper":
            raise RuntimeError("LIVE_SHADOW_REQUIRES_T100_EXECUTION_MODE_PAPER")
        if controls.live_orders_enabled:
            raise RuntimeError("LIVE_SHADOW_REFUSES_ORDER_API_ENABLED")

        self.config = config or ShadowRuntimeConfig.from_env()
        self.notifier = notifier or T100LiveOpsNotifier(
            os.getenv("DISCORD_TRADER_EVENTS_WEBHOOK_URL") or None,
            cooldown_seconds=self.config.notification_cooldown_seconds,
        )
        if self.config.require_discord and not self.notifier.configured:
            raise RuntimeError("LIVE_SHADOW_DISCORD_WEBHOOK_REQUIRED")

        if not os.getenv("MEXC_API_KEY") or not os.getenv("MEXC_API_SECRET"):
            raise RuntimeError("LIVE_SHADOW_MEXC_READ_CREDENTIALS_REQUIRED")

        self._preflight_runner = preflight_runner
        self._continuity_runner = continuity_runner
        self._clock = clock
        self.kill = OperationalKillLatch()
        self.stop_event = asyncio.Event()
        self._last_state: tuple[int, int, str] | None = None
        self._last_health_at: float | None = None
        self._kill_notified = False

    async def close(self) -> None:
        await self.notifier.close()

    def _trip(self, reason: str) -> None:
        if not self.kill.tripped:
            self.kill.trip(reason)
            LOGGER.warning("T100 live-shadow kill latched reason=%s", reason)

    async def _notify_kill_once(self) -> None:
        if not self.kill.tripped or self._kill_notified:
            return
        self._kill_notified = True
        await self.notifier.send(
            OpsNotice(
                key=f"kill:{self.kill.first_reason}",
                title="🛑 T100 LIVE-SHADOW KILL LATCHED",
                description=(
                    "Autonomous entry remains disabled. The shadow supervisor "
                    "will continue observing the account, but this process "
                    "cannot clear the kill latch."
                ),
                severity="critical",
                fields=(
                    {
                        "name": "Reason",
                        "value": str(self.kill.first_reason),
                        "inline": True,
                    },
                    {
                        "name": "Action",
                        "value": "Operator review + process restart required",
                        "inline": False,
                    },
                ),
            ),
            force=True,
        )

    async def _notify_state_change(
        self,
        *,
        positions: int,
        stops: int,
        protection: str,
    ) -> None:
        state = (positions, stops, protection)
        if self._last_state == state:
            return
        previous = self._last_state
        self._last_state = state
        await self.notifier.send(
            OpsNotice(
                key=f"account-state:{positions}:{stops}:{protection}",
                title="T100 LIVE-SHADOW • ACCOUNT STATE",
                description=(
                    "Observed exchange position/protection state changed."
                    if previous is not None
                    else "Initial exchange position/protection state observed."
                ),
                severity=(
                    "warning"
                    if positions > 0 and stops == 0
                    else "info"
                ),
                fields=(
                    {"name": "Positions", "value": str(positions), "inline": True},
                    {"name": "Current TP/SL", "value": str(stops), "inline": True},
                    {"name": "Protection", "value": protection, "inline": False},
                ),
            ),
            force=True,
        )

    async def _notify_health(self, report: ShadowCycleReport) -> None:
        now = self._clock()
        if (
            self._last_health_at is not None
            and now - self._last_health_at < self.config.health_report_seconds
        ):
            return
        self._last_health_at = now
        await self.notifier.send(
            OpsNotice(
                key="periodic-health",
                title="✅ T100 LIVE-SHADOW HEALTH",
                description=(
                    "Read-only Render supervisor is running. "
                    "This status does not authorize live trading."
                ),
                severity="success" if not report.kill_tripped else "warning",
                fields=(
                    {
                        "name": "Critical REST consistency",
                        "value": str(report.critical_inventory_consistent),
                        "inline": True,
                    },
                    {
                        "name": "REST symbol redundancy",
                        "value": str(report.symbol_redundancy_verified),
                        "inline": True,
                    },
                    {
                        "name": "Position margin witness",
                        "value": str(report.margin_witness_observed),
                        "inline": True,
                    },
                    {
                        "name": "REST↔WS continuity",
                        "value": report.continuity_status,
                        "inline": False,
                    },
                    {
                        "name": "Kill",
                        "value": (
                            str(report.kill_reason)
                            if report.kill_tripped
                            else "not latched"
                        ),
                        "inline": False,
                    },
                ),
            ),
            force=True,
        )

    async def cycle(self) -> ShadowCycleReport:
        preflight = await self._preflight_runner(
            include_historical_stop_schema=True,
            include_raw_consistency=True,
            include_funding_probe=False,
            include_symbol_crosscheck=True,
            include_position_margin_witness=True,
        )

        credential_status = str(preflight.get("credential_status") or "")
        read_failed = (
            preflight.get("read_failure_type") is not None
            or "SUCCEEDED" not in credential_status
        )
        critical_consistent = (
            preflight.get("critical_inventory_double_read_consistent") is True
        )
        positions = int(preflight.get("open_futures_positions") or 0)
        stops = int(preflight.get("open_futures_stop_orders") or 0)
        protection = str(
            preflight.get("t100_native_protection_presence") or "UNKNOWN"
        )
        crosscheck = preflight.get("symbol_scoped_inventory_crosscheck") or {}
        symbol_redundancy = (
            crosscheck.get("symbol_scoped_crosscheck_verified") is True
            if positions > 0
            else True
        )
        witness = preflight.get("position_inventory_economic_witness") or {}
        margin_witness = (
            witness.get("position_inventory_margin_witness_observed") is True
        )
        active_tpsl = preflight.get("active_tpsl_semantics") or {}
        protection_semantics_verified = (
            active_tpsl.get("active_tpsl_row_semantics_verified") is True
            if positions > 0 and stops > 0
            else positions == 0
        )
        if positions > 0 and stops > 0 and not protection_semantics_verified:
            LOGGER.warning(
                "T100 native protection semantic check failed "
                "status=%s state_untriggered=%s nonfinal=%s "
                "position_link=%s symbol_link=%s side_link=%s "
                "positive_stop_price=%s positive_volume=%s "
                "effective_quantity=%s every_position_covered=%s "
                "volume_covers=%s partial_rows=%s entire_rows=%s same_rows=%s "
                "separate_rows=%s unknown_vol_type=%s "
                "unknown_profit_loss_vol_type=%s",
                active_tpsl.get("status"),
                active_tpsl.get("all_rows_state_untriggered"),
                active_tpsl.get("all_rows_nonfinal_if_field_present"),
                active_tpsl.get("all_rows_link_to_observed_position"),
                active_tpsl.get("all_rows_symbol_link_consistent"),
                active_tpsl.get(
                    "all_rows_position_side_consistent_if_field_present"
                ),
                active_tpsl.get("all_rows_positive_stop_loss_price"),
                active_tpsl.get("all_rows_positive_protective_volume"),
                active_tpsl.get(
                    "all_rows_effective_protective_quantity_verified"
                ),
                active_tpsl.get("every_observed_position_has_current_tpsl"),
                active_tpsl.get(
                    "observed_stop_loss_volume_covers_positions"
                ),
                active_tpsl.get("partial_position_tpsl_rows"),
                active_tpsl.get("entire_position_tpsl_rows"),
                active_tpsl.get("same_volume_tpsl_rows"),
                active_tpsl.get("separate_volume_tpsl_rows"),
                active_tpsl.get("unknown_vol_type_rows"),
                active_tpsl.get("unknown_profit_loss_vol_type_rows"),
            )

        if read_failed:
            # Only internal status codes / exception class names. Never log
            # private response payloads, HTTP exception messages, URLs or IDs.
            LOGGER.warning(
                "T100 shadow preflight read failed "
                "failure_type=%s read_step=%s credential_status=%s "
                "inventory_consistency_failure_type=%s",
                str(preflight.get("read_failure_type") or "none")[:64],
                str(preflight.get("read_failure_step") or "unknown")[:64],
                credential_status[:96],
                str(
                    preflight.get("inventory_consistency_failure_type")
                    or "none"
                )[:64],
            )
            self._trip("READ_FAILURE")
        elif not critical_consistent:
            self._trip("INVENTORY_CHANGED_BETWEEN_READS")
        elif positions > 0 and not symbol_redundancy:
            self._trip("RECOVERY_UNVERIFIED")
        elif positions > 0 and not margin_witness:
            self._trip("RECOVERY_UNVERIFIED")
        elif positions > 0 and stops == 0:
            self._trip("NATIVE_PROTECTION_LOST")
        elif positions > 0 and stops > 0 and not protection_semantics_verified:
            self._trip("NATIVE_PROTECTION_UNVERIFIED")

        # Absent fields after a failed read are NOT evidence of a flat
        # account. Never advertise zero positions/protection from missing data.
        if not read_failed:
            await self._notify_state_change(
                positions=positions,
                stops=stops,
                protection=protection,
            )

        continuity = await self._continuity_runner(
            duration_seconds=self.config.ws_window_seconds
        )
        continuity_status = str(continuity.get("status") or "UNKNOWN")
        ws = continuity.get("ws_observation") or {}
        ws_events = int(ws.get("private_events_seen") or 0)

        if continuity_status == (
            "CRITICAL_REST_INVENTORY_CHANGED_DURING_WS_WINDOW"
        ):
            self._trip("INVENTORY_CHANGED_BETWEEN_READS")
        elif continuity_status in {
            "HALTED_REST_WS_REST_CONTINUITY",
            "STABLE_REST_WITH_UNEXPECTED_PRIVATE_WS_CHANNELS",
        }:
            LOGGER.warning(
                "T100 shadow continuity read failed "
                "status=%s failure_type=%s failure_stage=%s",
                continuity_status,
                str(continuity.get("failure_type") or "none")[:64],
                str(continuity.get("failure_stage") or "UNKNOWN")[:64],
            )
            self._trip("READ_FAILURE")

        if ws_events > 0:
            await self.notifier.send(
                OpsNotice(
                    key=f"ws-events:{self._clock()}",
                    title="⚡ T100 LIVE-SHADOW • PRIVATE ACCOUNT EVENT",
                    description=(
                        "MEXC private WebSocket reported an account lifecycle "
                        "event. REST reconciliation remained observation-only."
                    ),
                    severity="info",
                    fields=(
                        {
                            "name": "Position events",
                            "value": str(ws.get("position_events_seen") or 0),
                            "inline": True,
                        },
                        {
                            "name": "TP/SL events",
                            "value": str(ws.get("stop_plan_events_seen") or 0),
                            "inline": True,
                        },
                        {
                            "name": "Fill events",
                            "value": str(ws.get("fill_events_seen") or 0),
                            "inline": True,
                        },
                    ),
                ),
                force=True,
            )

        await self._notify_kill_once()

        snapshot = self.kill.snapshot()
        report = ShadowCycleReport(
            status=(
                "OBSERVING_KILL_LATCHED"
                if snapshot.tripped
                else "OBSERVING_NON_AUTHORIZING"
            ),
            positions=positions,
            current_tpsl=stops,
            protection_status=protection,
            critical_inventory_consistent=critical_consistent,
            symbol_redundancy_verified=symbol_redundancy,
            margin_witness_observed=margin_witness,
            continuity_status=continuity_status,
            ws_private_events=ws_events,
            kill_tripped=snapshot.tripped,
            kill_reason=snapshot.first_reason,
        )
        await self._notify_health(report)
        return report

    async def run(self) -> None:
        await self.notifier.send(
            OpsNotice(
                key="startup",
                title="👁️ T100 LIVE-SHADOW STARTED",
                description=(
                    "Render supervisor started in signed read-only mode. "
                    "Orders, cancels and stop mutations are unavailable."
                ),
                severity="info",
                fields=(
                    {
                        "name": "Execution mode",
                        "value": "paper / read-only shadow",
                        "inline": True,
                    },
                    {
                        "name": "Live authorization",
                        "value": "false",
                        "inline": True,
                    },
                ),
            ),
            force=True,
        )
        try:
            while not self.stop_event.is_set():
                started = self._clock()
                try:
                    await self.cycle()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOGGER.exception("T100 live-shadow cycle failed")
                    self._trip("READ_FAILURE")
                    await self._notify_kill_once()
                    await self.notifier.send(
                        OpsNotice(
                            key=f"cycle-error:{type(exc).__name__}",
                            title="🚨 T100 LIVE-SHADOW ERROR",
                            description=(
                                "A read-only supervision cycle failed. "
                                "Observation will retry; live authorization "
                                "remains false."
                            ),
                            severity="critical",
                            fields=(
                                {
                                    "name": "Failure type",
                                    "value": type(exc).__name__,
                                    "inline": True,
                                },
                            ),
                        )
                    )

                elapsed = self._clock() - started
                timeout = max(1.0, self.config.cycle_seconds - elapsed)
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(),
                        timeout=timeout,
                    )
                except TimeoutError:
                    pass
        finally:
            try:
                await self.notifier.send(
                    OpsNotice(
                        key="shutdown",
                        title="T100 LIVE-SHADOW STOPPED",
                        description=(
                            "Render shadow supervisor is shutting down. "
                            "No live-order state was enabled."
                        ),
                        severity="warning",
                    ),
                    force=True,
                )
            finally:
                await self.close()


async def main(*, once: bool = False) -> None:
    logging.basicConfig(
        level=getattr(
            logging,
            os.getenv("LOG_LEVEL", "INFO").upper(),
            logging.INFO,
        ),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    runtime = T100LiveShadowRuntime()
    loop = asyncio.get_running_loop()
    for system_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(system_signal, runtime.stop_event.set)

    if not once:
        await runtime.run()
        return

    try:
        report = await runtime.cycle()
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
        print(
            "NO ORDERS SENT. LIVE-SHADOW ONCE MODE DOES NOT ENABLE LIVE TRADING."
        )
    finally:
        await runtime.close()


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Continuous read-only T100 Render shadow supervisor"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one read-only supervision cycle, print redacted summary, exit",
    )
    args = parser.parse_args()
    asyncio.run(main(once=args.once))


if __name__ == "__main__":
    cli()

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.performance import should_send_daily_report
from app.t100_200_strategy import STRATEGY_ID
from app.t100_200_worker import M30, T100Worker, _floor_30m, configure_logging
from app.t100_performance import (
    FROZEN_COVERED_4Y_X,
    FROZEN_COVERED_CAGR_PCT,
    FROZEN_COVERED_END,
    FROZEN_COVERED_RETURN_PCT,
    FROZEN_COVERED_START,
    FROZEN_COVERED_TRIGGER_AWARE_DD_PCT,
    FROZEN_COVERED_WORST_SUBWINDOW_RETURN_PCT,
    FROZEN_FULL_4Y_X,
    FROZEN_FULL_CAGR_PCT,
    FROZEN_FULL_CLOSE_DD_PCT,
    FROZEN_FULL_RETURN_PCT,
    FROZEN_FULL_TRIGGER_AWARE_DD_PCT,
    build_t100_performance_summary,
)
from app.trader_notifier import TraderNotifier

LOGGER = logging.getLogger(__name__)


class T100Scanner(T100Worker):
    """Signal producer + subscriber reporting service.

    This service owns the frozen Min30 universe scan, P2 lifecycle, Daily-Core /
    Persistence admission, physical episode staging, t100_p2_signals writes, and
    the daily subscriber performance dashboard. It never admits portfolio
    positions and never applies exits/funding.
    """

    def __init__(self) -> None:
        super().__init__()
        self.performance_report_hour = int(os.getenv("PERFORMANCE_REPORT_HOUR", "18"))
        self.performance_report_timezone = os.getenv(
            "PERFORMANCE_REPORT_TIMEZONE", "Europe/Zurich"
        )
        self.performance_notifier = TraderNotifier(
            os.getenv("DISCORD_PERFORMANCE_WEBHOOK_URL") or None
        )

    async def close(self) -> None:
        await self.performance_notifier.close()
        await super().close()

    @staticmethod
    def _pct(value: float | None, *, signed: bool = False) -> str:
        if value is None:
            return "n/a"
        return f"{value:+.2f}%" if signed else f"{value:.2f}%"

    @staticmethod
    def _money(value: float) -> str:
        return f"${value:,.2f}"

    async def _maybe_send_daily_dashboard(self, now: datetime) -> None:
        runtime_row = await self.db.pool.fetchrow(
            "SELECT * FROM t100_runtime WHERE singleton=true"
        )
        if runtime_row is None:
            return
        runtime = dict(runtime_row)
        last_date = runtime.get("last_report_local_date")
        if not should_send_daily_report(
            now,
            timezone_name=self.performance_report_timezone,
            report_hour=self.performance_report_hour,
            already_sent_date=last_date,
        ):
            return

        position_rows = await self.db.pool.fetch(
            "SELECT * FROM t100_positions ORDER BY opened_at,id"
        )
        signal_rows = await self.db.pool.fetch(
            "SELECT * FROM t100_p2_signals ORDER BY p2_at,id"
        )
        snapshot_rows = await self.db.pool.fetch(
            "SELECT * FROM t100_equity_snapshots ORDER BY snapshot_at"
        )
        summary = build_t100_performance_summary(
            runtime=runtime,
            positions=[dict(row) for row in position_rows],
            signals=[dict(row) for row in signal_rows],
            snapshots=[dict(row) for row in snapshot_rows],
            now_utc=now,
            timezone_name=self.performance_report_timezone,
        )

        tz = ZoneInfo(self.performance_report_timezone)
        local_now = now.astimezone(tz)
        started_local = summary.started_at.astimezone(tz)
        win_rate_text = (
            "n/a"
            if summary.closed_win_rate is None
            else f"{summary.closed_win_rate:.2%}"
        )

        live = (
            f"Since **{started_local.strftime('%d %b %Y • %H:%M %Z')}** • "
            f"{summary.span_days:.2f}d\n"
            f"Equity **{self._money(summary.current_equity_usdt)}** from "
            f"**{self._money(summary.starting_equity_usdt)}** • MTM "
            f"**{self._pct(summary.total_return_pct, signed=True)}** • 30D equivalent "
            f"**{self._pct(summary.thirty_day_equivalent_pct, signed=True)}***\n"
            f"Realized **{self._money(summary.realized_pnl_usdt)}** • unrealized "
            f"**{self._money(summary.unrealized_pnl_usdt)}** • open **{summary.open_positions}**\n"
            f"Closed **{summary.closed_wins}W / {summary.closed_losses}L** • win rate "
            f"**{win_rate_text}** • close DD "
            f"**{self._pct(summary.close_max_drawdown_pct, signed=True)}**\n"
            f"Gross exposure now **{self._pct(summary.current_gross_exposure_pct)}** • "
            f"avg/peak **{self._pct(summary.avg_gross_exposure_pct)} / "
            f"{self._pct(summary.peak_gross_exposure_pct)}**"
        )

        economics = (
            f"Entries LOW_100 **{summary.low_entries}** • HIGH_200 **{summary.high_entries}**\n"
            f"Funding **{self._money(summary.funding_net_usdt)}** • fees "
            f"**{self._money(summary.fees_usdt)}** • execution debit "
            f"**{self._money(summary.slippage_usdt)}**\n"
            f"Median/worst adverse excursion **{self._pct(summary.median_adverse_pct)} / "
            f"{self._pct(summary.worst_adverse_pct)}**\n"
            f"Exits: SL75 **{summary.sl75_exits}** • trailing **{summary.trail_exits}**"
        )

        funnel = (
            f"Raw admitted P2 **{summary.raw_p2}** • Stage1 **{summary.stage1_count}** • "
            f"eligible Stage2 **{summary.eligible_stage2}** • entered **{summary.accepted_stage2}**\n"
            f"Skipped capacity **{summary.ignored_capacity}** • duplicate symbol "
            f"**{summary.ignored_duplicate_symbol}** • invalid **{summary.ignored_invalid}** • "
            f"no equity **{summary.ignored_no_equity}**\n"
            f"Today: eligible **{summary.today_eligible_stage2}** • entries "
            f"**{summary.today_entries}** • exits **{summary.today_exits}** • closed net "
            f"**{self._money(summary.today_net_closed_pnl_usdt)}**"
        )

        open_positions = (
            "\n".join(summary.open_lines)
            if summary.open_lines
            else "No open T100_200 paper positions."
        )

        benchmark = (
            f"Full known-funding history: return **+{FROZEN_FULL_RETURN_PCT:.1f}%** • "
            f"CAGR **{FROZEN_FULL_CAGR_PCT:.1f}%** • 4Y **{FROZEN_FULL_4Y_X:.1f}×** • "
            f"trigger-aware DD **{FROZEN_FULL_TRIGGER_AWARE_DD_PCT:.1f}%** • "
            f"close DD **{FROZEN_FULL_CLOSE_DD_PCT:.1f}%**\n"
            f"Fully covered MEXC window **{FROZEN_COVERED_START} → {FROZEN_COVERED_END}**: "
            f"return **+{FROZEN_COVERED_RETURN_PCT:.1f}%** • CAGR "
            f"**{FROZEN_COVERED_CAGR_PCT:.1f}%** • 4Y extrapolation "
            f"**{FROZEN_COVERED_4Y_X:.1f}×** • trigger-aware DD "
            f"**{FROZEN_COVERED_TRIGGER_AWARE_DD_PCT:.1f}%** • 3/3 covered subwindows "
            f"positive, worst **+{FROZEN_COVERED_WORST_SUBWINDOW_RETURN_PCT:.1f}%**\n"
            "Full-history funding before the public MEXC retention floor is incomplete; "
            "covered-window 4Y is an extrapolation, not a forecast."
        )

        running = (
            "**Stage2 P15_A4_D0** • native Min30 P2 • 4 slots • one position/symbol\n"
            "Stage1 strict365 + r24 ≥15% + ATR7 ≥4% • LOW_100 <5.7% ATR → "
            "25% equity notional • HIGH_200 ≥5.7% → 50%\n"
            "SL75 before trail • trail arms +10% • 1pp gap • ADVERSE_FIRST • "
            "0.08%/fill • 25bp each-side execution debit • funding included"
        )

        sent = await self.performance_notifier.send(
            "📊 T100_200 • Daily Performance Dashboard",
            (
                f"Updated **{local_now.strftime('%d %b %Y • %H:%M %Z')}**\n"
                "Live paper performance is separated from the frozen historical benchmark."
            ),
            [
                {"name": "🟢 1 • Live Paper Portfolio", "value": live, "inline": False},
                {"name": "💵 2 • Economics & Risk", "value": economics, "inline": False},
                {"name": "🎯 3 • Signal Funnel", "value": funnel, "inline": False},
                {"name": "📍 4 • Open Positions", "value": open_positions, "inline": False},
                {"name": "🧪 5 • Frozen Certification Benchmark", "value": benchmark, "inline": False},
                {"name": "⚙️ Running Strategy", "value": running, "inline": False},
            ],
            color=0x5865F2,
        )
        if not sent:
            LOGGER.warning("T100 daily performance dashboard not sent; will retry")
            return

        await self.db.pool.execute(
            """
            UPDATE t100_runtime
            SET last_report_local_date=$1,updated_at=now()
            WHERE singleton=true
            """,
            local_now.date(),
        )
        LOGGER.info(
            "T100 daily performance dashboard sent report_date=%s equity=%.2f return=%.2f%%",
            local_now.date(),
            summary.current_equity_usdt,
            summary.total_return_pct,
        )

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

                try:
                    await self._maybe_send_daily_dashboard(datetime.now(UTC))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception("T100 daily performance dashboard check failed")

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

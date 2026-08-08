from __future__ import annotations

import logging

import httpx

from app.models import RunSignal
from app.performance import PerformanceSummary

LOGGER = logging.getLogger(__name__)


class DiscordNotifier:
    def __init__(self, webhook_url: str | None) -> None:
        self._webhook_url = webhook_url
        self._client = httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def send_signal(self, signal: RunSignal) -> None:
        if not self._webhook_url:
            return

        features = signal.features
        run_score = features.get("run_score", signal.score)
        exhaustion_score = features.get("exhaustion_score")
        if signal.level == "confirmed_short":
            title = f"🚨 **{signal.symbol} — CONFIRMED SHORT**"
        elif signal.level == "breakdown_watch":
            title = f"🔴 **{signal.symbol} — BREAKDOWN WATCH**"
        elif signal.level == "exhaustion_watch":
            title = f"🟠 **{signal.symbol} — EXHAUSTION WATCH**"
        else:
            title = f"🟡 **{signal.symbol} — RUN WATCH**"

        lines = [
            title,
            f"Episode: #{signal.episode_id}" if signal.episode_id is not None else "Episode: n/a",
            f"Run score: {run_score}/6",
            f"24h: {self._percent(features.get('return_24h'))}",
            f"72h: {self._percent(features.get('return_72h'))}",
            f"BTC residual: {self._percent(features.get('residual_return_24h'))}",
            f"1h momentum: {self._percent(features.get('momentum_1h'))}",
            f"Volume z-score: {self._number(features.get('volume_zscore_15m'))}",
            f"EMA distance: {self._number(features.get('distance_above_ema20_atr_4h'))} ATR",
            f"Funding: {self._percent(features.get('funding_rate'))}",
        ]
        if signal.level in {"exhaustion_watch", "breakdown_watch", "confirmed_short"}:
            lines.append(
                f"Exhaustion score: {exhaustion_score if exhaustion_score is not None else 'n/a'}/7"
            )
        if features.get("episode_peak_price") is not None:
            lines.append(f"Episode peak: {self._price(features.get('episode_peak_price'))}")
        if signal.level in {"breakdown_watch", "confirmed_short"}:
            lines.append(f"Broken level: {self._price(features.get('broken_level'))}")
        if signal.level == "breakdown_watch":
            lines.append(
                f"Retest window: {features.get('retest_window_candles', 'n/a')} × 15m candles"
            )
            lines.append(
                f"Retest tolerance: {self._number(features.get('retest_tolerance_atr'))} ATR"
            )
        if signal.level == "confirmed_short":
            lines.append(f"Retest high: {self._price(features.get('retest_high'))}")
            lines.append(f"Retest close: {self._price(features.get('retest_close'))}")
            lines.append("Episode locked: YES — no second short alert unless a new episode re-arms")

        lines.extend(
            [
                "Reasons: " + "; ".join(signal.reasons),
                "Shadow mode only — no order is placed.",
            ]
        )
        try:
            response = await self._client.post(
                self._webhook_url, json={"content": "\n".join(lines)}
            )
            response.raise_for_status()
        except httpx.HTTPError:
            LOGGER.exception("Discord alert failed for %s", signal.symbol)

    async def send_performance_report(self, report: PerformanceSummary) -> bool:
        if not self._webhook_url:
            return False

        lines = [
            f"📊 **DAILY SHADOW PERFORMANCE — {report.report_date.isoformat()}**",
            f"Confirmed shorts today: {report.confirmed_today}",
            f"Open tracked signals: {report.open_count}",
            (
                "Open mark-to-market: "
                f"{self._percent(report.open_avg_return)} avg | "
                f"{self._percent(report.open_sum_return)} summed"
            ),
            f"24h matured signals: {report.matured_total} all-time | {report.matured_today} today",
            f"24h win rate: {self._percent(report.win_rate_24h)}",
            (
                "Average short return: "
                f"1h {self._percent(report.avg_return_1h)} | "
                f"4h {self._percent(report.avg_return_4h)} | "
                f"12h {self._percent(report.avg_return_12h)} | "
                f"24h {self._percent(report.avg_return_24h)}"
            ),
            f"Summed 24h signal return: {self._percent(report.sum_return_24h)}",
            f"Average MFE: {self._percent(report.avg_mfe)} | Average MAE: {self._percent(report.avg_mae)}",
        ]
        if report.best_symbol is not None:
            lines.append(
                f"Best 24h: {report.best_symbol} {self._percent(report.best_return_24h)}"
            )
        if report.worst_symbol is not None:
            lines.append(
                f"Worst 24h: {report.worst_symbol} {self._percent(report.worst_return_24h)}"
            )
        lines.extend(
            [
                "Returns are measured from CONFIRMED SHORT retest close.",
                "Analytics only: no fees, slippage, funding, leverage or position sizing included.",
            ]
        )
        try:
            response = await self._client.post(
                self._webhook_url, json={"content": "\n".join(lines)}
            )
            response.raise_for_status()
            return True
        except httpx.HTTPError:
            LOGGER.exception("Discord performance report failed")
            return False

    @staticmethod
    def _percent(value: object) -> str:
        return "n/a" if value is None else f"{float(value):.2%}"

    @staticmethod
    def _number(value: object) -> str:
        return "n/a" if value is None else f"{float(value):.2f}"

    @staticmethod
    def _price(value: object) -> str:
        if value is None:
            return "n/a"
        number = float(value)
        if abs(number) >= 1000:
            return f"{number:,.2f}"
        if abs(number) >= 1:
            return f"{number:.6f}".rstrip("0").rstrip(".")
        return f"{number:.10f}".rstrip("0").rstrip(".")

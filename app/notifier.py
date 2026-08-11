from __future__ import annotations

import logging
from datetime import datetime

import httpx

from app.models import RunSignal
from app.performance import PerformanceSummary

LOGGER = logging.getLogger(__name__)


class DiscordNotifier:
    def __init__(
        self,
        webhook_url: str | None,
        signal_levels: frozenset[str] | set[str] | None = None,
    ) -> None:
        self._webhook_url = webhook_url
        self._signal_levels = frozenset(
            signal_levels or {"exhaustion_watch", "confirmed_short"}
        )
        self._client = httpx.AsyncClient(timeout=15.0)

    def should_send_signal(self, level: str) -> bool:
        return level in self._signal_levels

    async def close(self) -> None:
        await self._client.aclose()

    async def send_signal(self, signal: RunSignal) -> None:
        if not self._webhook_url or not self.should_send_signal(signal.level):
            return

        features = signal.features
        run_score = features.get("run_score", signal.score)
        exhaustion_score = features.get("exhaustion_score")
        risk_tier = str(features.get("risk_tier") or "standard")
        risk_warning = features.get("execution_risk_warning")
        if signal.level == "confirmed_short":
            title = f"🚨 **{signal.symbol} — CONFIRMED SHORT**"
        elif signal.level == "breakdown_watch":
            title = f"🔴 **{signal.symbol} — BREAKDOWN WATCH**"
        elif signal.level == "exhaustion_watch":
            title = f"🟠 **{signal.symbol} — EXHAUSTION WATCH**"
        else:
            title = f"🟡 **{signal.symbol} — RUN WATCH**"

        lines = [title]
        if risk_tier == "high_risk":
            lines.extend(
                [
                    "⚠️ **HIGH-RISK / LOW-LIQUIDITY CANDIDATE**",
                    "Execution-quality filter: FAIL — signal remains visible for research.",
                ]
            )
        elif risk_tier == "extreme_risk":
            lines.extend(
                [
                    "⛔ **EXTREME EXECUTION RISK**",
                    "Analytics only — thin liquidity/spread can make this impractical to short safely.",
                ]
            )
        else:
            lines.append("🟢 Execution quality: STANDARD")
        if risk_warning and risk_tier != "standard":
            lines.append(str(risk_warning))
        lines.extend([
            f"24h futures turnover: {self._money(features.get('amount_24h'))}",
            f"Bid/ask spread: {self._spread(features.get('spread_pct'))}",
            f"Episode: #{signal.episode_id}" if signal.episode_id is not None else "Episode: n/a",
            f"Run score: {run_score}/6",
            f"24h: {self._percent(features.get('return_24h'))}",
            f"72h: {self._percent(features.get('return_72h'))}",
            f"BTC residual: {self._percent(features.get('residual_return_24h'))}",
            f"1h momentum: {self._percent(features.get('momentum_1h'))}",
            f"Volume z-score: {self._number(features.get('volume_zscore_15m'))}",
            f"EMA distance: {self._number(features.get('distance_above_ema20_atr_4h'))} ATR",
            f"Funding: {self._percent(features.get('funding_rate'))}",
        ])
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

        risk_reasons = features.get("execution_risk_reasons")
        if risk_reasons and risk_tier != "standard":
            lines.append("Risk flags: " + "; ".join(str(item) for item in risk_reasons))

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

    async def send_performance_report(
        self,
        report: PerformanceSummary,
        *,
        label: str = "DAILY SHADOW PERFORMANCE",
        as_of: datetime | None = None,
        timezone_name: str | None = None,
    ) -> bool:
        if not self._webhook_url:
            return False

        lines = [f"📊 **{label} — {report.report_date.isoformat()}**"]
        if as_of is not None:
            display = as_of
            if timezone_name:
                from zoneinfo import ZoneInfo
                display = as_of.astimezone(ZoneInfo(timezone_name))
            lines.append(f"As of: {display.strftime('%Y-%m-%d %H:%M:%S %Z')}")

        lines.extend([
            f"Confirmed shorts today: {report.confirmed_today}",
            f"Open tracked signals (until 72h): {report.open_count}",
            (
                "Open mark-to-market: "
                f"{self._percent(report.open_avg_return)} avg | "
                f"{self._percent(report.open_sum_return)} summed"
            ),
        ])

        for horizon in (report.horizon_24h, report.horizon_48h, report.horizon_72h):
            lines.append(
                f"{horizon.hours}h: {horizon.matured_total} matured "
                f"({horizon.matured_today} today) | win {self._percent(horizon.win_rate)} | "
                f"avg {self._percent(horizon.avg_return)} | sum {self._percent(horizon.sum_return)}"
            )

        lines.append("Performance by execution risk:")
        for horizon in (report.horizon_24h, report.horizon_48h, report.horizon_72h):
            lines.append(
                f"{horizon.hours}h STANDARD — n={horizon.standard_total} | "
                f"win {self._percent(horizon.standard_win_rate)} | "
                f"avg {self._percent(horizon.standard_avg_return)} | "
                f"sum {self._percent(horizon.standard_sum_return)}"
            )
            lines.append(
                f"{horizon.hours}h HIGH+EXTREME — n={horizon.high_risk_total} | "
                f"win {self._percent(horizon.high_risk_win_rate)} | "
                f"avg {self._percent(horizon.high_risk_avg_return)} | "
                f"sum {self._percent(horizon.high_risk_sum_return)}"
            )

        lines.extend([
            (
                "Average short-return path: "
                f"1h {self._percent(report.avg_return_1h)} | "
                f"4h {self._percent(report.avg_return_4h)} | "
                f"12h {self._percent(report.avg_return_12h)} | "
                f"24h {self._percent(report.avg_return_24h)} | "
                f"48h {self._percent(report.avg_return_48h)} | "
                f"72h {self._percent(report.avg_return_72h)}"
            ),
            (
                "72h excursion (fully matured only): "
                f"MFE {self._percent(report.avg_mfe_72h)} | "
                f"MAE {self._percent(report.avg_mae_72h)}"
            ),
        ])
        if report.best_symbol_72h is not None:
            lines.append(
                f"Best 72h: {report.best_symbol_72h} {self._percent(report.best_return_72h)}"
            )
        if report.worst_symbol_72h is not None:
            lines.append(
                f"Worst 72h: {report.worst_symbol_72h} {self._percent(report.worst_return_72h)}"
            )
        lines.extend([
            "Returns are measured from CONFIRMED SHORT retest close.",
            "Analytics only: no fees, slippage, funding, leverage or position sizing included.",
        ])

        content = "\n".join(lines)
        if len(content) > 1950:
            LOGGER.warning("Performance report content is long (%d chars)", len(content))
        try:
            response = await self._client.post(self._webhook_url, json={"content": content})
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
    def _money(value: object) -> str:
        if value is None:
            return "n/a"
        number = float(value)
        if number >= 1_000_000_000:
            return f"${number / 1_000_000_000:.2f}B"
        if number >= 1_000_000:
            return f"${number / 1_000_000:.2f}M"
        if number >= 1_000:
            return f"${number / 1_000:.1f}K"
        return f"${number:,.0f}"

    @staticmethod
    def _spread(value: object) -> str:
        return "n/a" if value is None else f"{float(value):.3f}%"

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


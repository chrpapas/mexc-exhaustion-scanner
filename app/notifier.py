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

        title = f"📊 **{label} — {report.report_date.isoformat()}**"
        if as_of is not None:
            display = as_of
            if timezone_name:
                from zoneinfo import ZoneInfo
                display = as_of.astimezone(ZoneInfo(timezone_name))
            title += f"\nAs of: {display.strftime('%Y-%m-%d %H:%M:%S %Z')}"

        main = [
            title,
            f"Confirmed shorts today: {report.confirmed_today}",
            f"Open tracked signals (until 7d): {report.open_count}",
            f"Open mark-to-market: {self._percent(report.open_avg_return)} avg | {self._percent(report.open_sum_return)} summed",
        ]
        for horizon in (report.horizon_24h, report.horizon_48h, report.horizon_72h, report.horizon_168h):
            label_h = "7d" if horizon.hours == 168 else f"{horizon.hours}h"
            main.append(
                f"{label_h}: {horizon.matured_total} matured ({horizon.matured_today} today) | "
                f"win {self._percent(horizon.win_rate)} | avg {self._percent(horizon.avg_return)} | "
                f"sum {self._percent(horizon.sum_return)}"
            )

        main.append("Performance by execution risk:")
        for horizon in (report.horizon_24h, report.horizon_48h, report.horizon_72h, report.horizon_168h):
            label_h = "7d" if horizon.hours == 168 else f"{horizon.hours}h"
            main.append(
                f"{label_h} STANDARD — n={horizon.standard_total} | win {self._percent(horizon.standard_win_rate)} | "
                f"avg {self._percent(horizon.standard_avg_return)} | sum {self._percent(horizon.standard_sum_return)}"
            )
            main.append(
                f"{label_h} HIGH+EXTREME — n={horizon.high_risk_total} | win {self._percent(horizon.high_risk_win_rate)} | "
                f"avg {self._percent(horizon.high_risk_avg_return)} | sum {self._percent(horizon.high_risk_sum_return)}"
            )

        main.extend([
            f"Average short-return path: 1h {self._percent(report.avg_return_1h)} | 4h {self._percent(report.avg_return_4h)} | "
            f"12h {self._percent(report.avg_return_12h)} | 24h {self._percent(report.avg_return_24h)} | "
            f"48h {self._percent(report.avg_return_48h)} | 72h {self._percent(report.avg_return_72h)} | "
            f"7d {self._percent(report.avg_return_168h)}",
            f"7d excursion (fully matured only): MFE {self._percent(report.avg_mfe_7d)} | MAE {self._percent(report.avg_mae_7d)}",
        ])
        if report.best_symbol_7d:
            main.append(f"Best 7d: {report.best_symbol_7d} {self._percent(report.best_return_7d)}")
        if report.worst_symbol_7d:
            main.append(f"Worst 7d: {report.worst_symbol_7d} {self._percent(report.worst_return_7d)}")

        def weekly_line(summary) -> list[str]:
            return [
                f"**{summary.risk_label} — 7d path survival (n={summary.matured_7d})**",
                f"Ever profitable within 7d: {self._percent(summary.ever_profitable_rate)}",
                f"Reached +20% short return within 7d: {self._percent(summary.hit_20_rate)}",
                f"Hit +100% adverse move: {self._percent(summary.isolated_100_breach_rate)}  ← 1x isolated-loss proxy",
                f"Hit +400% adverse move: {self._percent(summary.cross_400_breach_rate)}  ← configured 5× cross-buffer breach",
                f"+100% adverse before first profit: {self._percent(summary.isolated_breach_before_profit_rate)}",
                f"+400% adverse before first profit: {self._percent(summary.cross_breach_before_profit_rate)}",
                f"+400% adverse before +20% target: {self._percent(summary.cross_breach_before_20_rate)}",
            ]

        survival = ["🧪 **THEORETICAL CAPITAL-BUFFER SIMULATION**"]
        survival.extend(weekly_line(report.standard_weekly))
        survival.extend(weekly_line(report.risky_weekly))
        survival.append("Returns among trades that had NOT breached the +400% adverse threshold by each horizon:")
        for std, risky in zip(report.standard_survivors, report.risky_survivors):
            label_h = "7d" if std.hours == 168 else f"{std.hours}h"
            survival.append(
                f"{label_h} STANDARD — survive {std.survived_cross_buffer}/{std.matured_total} "
                f"({self._percent(std.survival_rate)}) | win {self._percent(std.win_rate)} | "
                f"avg {self._percent(std.avg_return)} | sum {self._percent(std.sum_return)} | "
                f"20%-sized acct equiv {self._percent(std.account_equivalent_sum_return)}"
            )
            survival.append(
                f"{label_h} HIGH+EXTREME — survive {risky.survived_cross_buffer}/{risky.matured_total} "
                f"({self._percent(risky.survival_rate)}) | win {self._percent(risky.win_rate)} | "
                f"avg {self._percent(risky.avg_return)} | sum {self._percent(risky.sum_return)} | "
                f"20%-sized acct equiv {self._percent(risky.account_equivalent_sum_return)}"
            )
        survival.extend([
            "Thresholds are research proxies, not MEXC liquidation prices. Actual liquidation occurs earlier/later depending on maintenance margin, fees, other cross positions and account equity.",
            "20%-sized account equivalent = 0.20 × summed position returns; it is NOT a compounding/overlap-aware portfolio backtest.",
        ])

        try:
            for lines in (main, survival):
                content = "\n".join(lines)
                # Discord webhook content limit is 2000 chars. Split cleanly if needed.
                while len(content) > 1900:
                    cut = content.rfind("\n", 0, 1900)
                    if cut <= 0:
                        cut = 1900
                    part, content = content[:cut], content[cut:].lstrip("\n")
                    response = await self._client.post(self._webhook_url, json={"content": part})
                    response.raise_for_status()
                if content:
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


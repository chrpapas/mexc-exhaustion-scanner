from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.models import RunSignal
from app.signal_ledger import SignalLedger, SignalLedgerItem
from app.performance import (
    HorizonSummary,
    HorizonSurvivalSummary,
    PerformanceSummary,
    ProfitTargetSummary,
    StrategyMatrixSummary,
    StrategyRowSummary,
    StrategyThresholdSummary,
    WeeklyRiskSummary,
)

LOGGER = logging.getLogger(__name__)


class DiscordNotifier:
    def __init__(
        self,
        webhook_url: str | None,
        signal_levels: frozenset[str] | set[str] | None = None,
        *,
        performance_webhook_url: str | None = None,
    ) -> None:
        self._webhook_url = webhook_url
        # Backward-compatible fallback: if the dedicated stats webhook is not
        # configured, reports continue to go to the existing Discord webhook.
        self._performance_webhook_url = performance_webhook_url or webhook_url
        self._signal_levels = frozenset(signal_levels or {"confirmed_short"})
        self._client = httpx.AsyncClient(timeout=15.0)

    def should_send_signal(self, level: str) -> bool:
        # Discord is intentionally short-only. Intermediate strategy states
        # (run/exhaustion/breakdown) remain internal even if an older Render
        # environment still lists them in DISCORD_SIGNAL_LEVELS.
        return level == "confirmed_short" and level in self._signal_levels

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
        title = f"🚨 **{signal.symbol} — CONFIRMED SHORT**"

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
        lines.extend(
            [
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
                f"Exhaustion score: {exhaustion_score if exhaustion_score is not None else 'n/a'}/7",
            ]
        )
        if features.get("episode_peak_price") is not None:
            lines.append(f"Episode peak: {self._price(features.get('episode_peak_price'))}")
        lines.extend(
            [
                f"Broken level: {self._price(features.get('broken_level'))}",
                f"Retest high: {self._price(features.get('retest_high'))}",
                f"Retest close: {self._price(features.get('retest_close'))}",
                "Episode locked: YES — no second short alert unless a new episode re-arms",
            ]
        )

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
                self._webhook_url,
                json={"content": "\n".join(lines), "allowed_mentions": {"parse": []}},
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
        if not self._performance_webhook_url:
            return False

        display_time = as_of
        if display_time is not None and timezone_name:
            display_time = display_time.astimezone(ZoneInfo(timezone_name))
        as_of_text = (
            display_time.strftime("%d %b %Y • %H:%M %Z")
            if display_time is not None
            else report.report_date.strftime("%d %b %Y")
        )

        overview = {
            "title": "📊 Exhaustion Scanner • Performance Board",
            "description": (
                f"**{self._pretty_label(label)}**\n"
                f"Updated **{as_of_text}**\n\n"
                "Confirmed-short signals only • Shadow analytics • Positive return = profitable short"
            ),
            "color": 0x5865F2,
            "fields": [
                {
                    "name": "⚡ Activity",
                    "value": (
                        f"**{report.confirmed_today}** confirmed today\n"
                        f"**{report.open_count}** still tracking to 7d"
                    ),
                    "inline": True,
                },
                {
                    "name": "📈 Open Signals",
                    "value": (
                        f"Avg MTM **{self._signed_percent(report.open_avg_return)}**\n"
                        f"Combined **{self._signed_percent(report.open_sum_return)}**"
                    ),
                    "inline": True,
                },
                {
                    "name": "🎯 Raw Results — All Signals",
                    "value": "\n".join(
                        self._raw_horizon_line(h)
                        for h in self._horizons(report)
                    ),
                    "inline": False,
                },
                {
                    "name": "⏱️ Average Return Path",
                    "value": (
                        f"1h **{self._signed_percent(report.avg_return_1h)}**  •  "
                        f"4h **{self._signed_percent(report.avg_return_4h)}**  •  "
                        f"12h **{self._signed_percent(report.avg_return_12h)}**\n"
                        f"1d **{self._signed_percent(report.avg_return_24h)}**  •  "
                        f"2d **{self._signed_percent(report.avg_return_48h)}**  •  "
                        f"3d **{self._signed_percent(report.avg_return_72h)}**  •  "
                        f"7d **{self._signed_percent(report.avg_return_168h)}**"
                    ),
                    "inline": False,
                },
            ],
            "footer": {
                "text": "Raw signal analytics — no take-profit, stop-loss, leverage or position-sizing rule assumed."
            },
        }

        if report.avg_mfe_7d is not None or report.avg_mae_7d is not None:
            overview["fields"].append(
                {
                    "name": "🌊 7-Day Excursion • Fully Matured Only",
                    "value": (
                        f"Avg favorable move (MFE) **{self._signed_percent(report.avg_mfe_7d)}**  •  "
                        f"Avg adverse move (MAE) **{self._signed_percent(report.avg_mae_7d)}**"
                    ),
                    "inline": False,
                }
            )
        if report.best_symbol_7d or report.worst_symbol_7d:
            overview["fields"].append(
                {
                    "name": "🏆 7-Day Extremes",
                    "value": (
                        f"Best: **{report.best_symbol_7d or 'n/a'}** {self._signed_percent(report.best_return_7d)}\n"
                        f"Worst: **{report.worst_symbol_7d or 'n/a'}** {self._signed_percent(report.worst_return_7d)}"
                    ),
                    "inline": False,
                }
            )

        standard = self._risk_embed(
            title="🟢 STANDARD Execution Risk",
            color=0x57F287,
            matrix=report.standard_strategy_matrix,
            weekly=report.standard_weekly,
        )
        high = self._risk_embed(
            title="🟡 HIGH RISK Execution Risk",
            color=0xFEE75C,
            matrix=report.high_strategy_matrix,
            weekly=report.high_weekly,
        )
        extreme = self._risk_embed(
            title="🔴 EXTREME RISK Execution Risk",
            color=0xED4245,
            matrix=report.extreme_strategy_matrix,
            weekly=report.extreme_weekly,
        )
        methodology = {
            "title": "🧭 Strategy Matrix — How to Read It",
            "description": (
                "Choose a target row, then a maximum adverse-loss threshold. The displayed WR is the percentage "
                "that achieved that target without first crossing the selected threshold."
            ),
            "color": 0x99AAB5,
            "fields": [
                {
                    "name": "Loss thresholds",
                    "value": (
                        "**-100%** = price reaches 2× entry • **-200%** = 3× • "
                        "**-300%** = 4× • **-400%** = 5×."
                    ),
                    "inline": False,
                },
                {
                    "name": "🎯 +20% target",
                    "value": (
                        "Horizon-independent race. Pending signals remain pending. WR uses resolved target-vs-breach "
                        "outcomes; same-15m-candle target/breach is conservatively breach-first."
                    ),
                    "inline": False,
                },
                {
                    "name": "⏱️ 1D / 2D / 3D / 7D profitable",
                    "value": (
                        "A win means the short return is **positive at that exact horizon** and the selected adverse "
                        "threshold was never crossed beforehand. Losses are split into **breached before maturity** "
                        "versus **not profitable at maturity**; these categories are mutually exclusive."
                    ),
                    "inline": False,
                },
                {
                    "name": "💯 100% strategy rows",
                    "value": (
                        "When a cell has **100% WR**, the board also shows average and summed profit for that exact "
                        "strategy/threshold combination. For +20% target, profit is modeled at the +20% exit target."
                    ),
                    "inline": False,
                },
                {
                    "name": "Important",
                    "value": (
                        "These are **research thresholds, not exact exchange liquidation prices**. Actual liquidation "
                        "depends on maintenance margin, fees, contract tier, equity and margin configuration."
                    ),
                    "inline": False,
                },
            ],
            "footer": {"text": "No fees, slippage, funding, leverage or overlapping-position portfolio effects included."},
        }

        # Discord limits the combined textual content across all embeds in one
        # message to 6,000 characters. The strategy matrices can legitimately
        # exceed that when combined with the overview and methodology, so send
        # each visual card as its own webhook message. This keeps the same
        # subscriber-facing board while giving every card its own embed budget.
        embeds = (overview, standard, high, extreme, methodology)
        try:
            for index, embed in enumerate(embeds, start=1):
                self._validate_discord_embed(embed)
                payload = {
                    "username": "Exhaustion Scanner • Stats",
                    "embeds": [embed],
                    "allowed_mentions": {"parse": []},
                }
                response = await self._client.post(self._performance_webhook_url, json=payload)
                if response.status_code >= 400:
                    LOGGER.error(
                        "Discord performance card %d/%d rejected status=%s body=%s",
                        index,
                        len(embeds),
                        response.status_code,
                        response.text[:2000],
                    )
                response.raise_for_status()
            return True
        except (httpx.HTTPError, ValueError):
            LOGGER.exception("Discord performance report failed")
            return False


    async def send_signal_ledger(
        self,
        ledger: SignalLedger,
        *,
        csv_bytes: bytes | None = None,
        as_of: datetime | None = None,
        timezone_name: str = "Europe/Zurich",
    ) -> bool:
        """Send a subscriber-facing per-signal outcome ledger.

        The ledger is intentionally separate from the aggregate performance board.
        It uses the same performance webhook and sends a compact summary followed
        by paginated risk-tier cards. A CSV attachment contains the full raw table.
        """
        if not self._performance_webhook_url:
            return False

        tz = ZoneInfo(timezone_name)
        display_time = (as_of or ledger.generated_at).astimezone(tz)
        risk_counts = {
            "standard": len(ledger.by_risk("standard")),
            "high_risk": len(ledger.by_risk("high_risk")),
            "extreme_risk": len(ledger.by_risk("extreme_risk")),
        }
        target_before_100 = sum(item.target_before_100_breach is True for item in ledger.items)
        breach_before_target = sum(
            item.target_before_100_breach is False and item.first_100_breach_at is not None
            for item in ledger.items
        )
        pending_target_race = ledger.total - target_before_100 - breach_before_target

        summary = {
            "title": "📒 Exhaustion Scanner • Signal Outcome Ledger",
            "description": (
                f"Updated **{display_time.strftime('%d %b %Y • %H:%M %Z')}**\n"
                "Every confirmed-short signal shown individually • newest first"
            ),
            "color": 0x5865F2,
            "fields": [
                {
                    "name": "📦 Signals",
                    "value": (
                        f"**{ledger.total}** total\n"
                        f"🟢 STANDARD **{risk_counts['standard']}** • "
                        f"🟡 HIGH **{risk_counts['high_risk']}** • "
                        f"🔴 EXTREME **{risk_counts['extreme_risk']}**"
                    ),
                    "inline": False,
                },
                {
                    "name": "🎯 +20% vs -100% Path",
                    "value": (
                        f"🟢 Target first **{target_before_100}** • "
                        f"🔴 -100% breach first **{breach_before_target}** • "
                        f"🔵 unresolved **{pending_target_race}**"
                    ),
                    "inline": False,
                },
                {
                    "name": "💥 Observed Adverse Breaches",
                    "value": (
                        f"🔴 -100% **{ledger.count_breach(100)}** • "
                        f"🟥 -200% **{ledger.count_breach(200)}** • "
                        f"🟣 -300% **{ledger.count_breach(300)}** • "
                        f"⚫ -400% **{ledger.count_breach(400)}**"
                    ),
                    "inline": False,
                },
                {
                    "name": "Legend",
                    "value": (
                        "🟢 target/profitable • 🟡 profitable but <20% • "
                        "⚪ negative but no -100% breach • 🔴/🟥/🟣/⚫ breach severity • 🔵 pending"
                    ),
                    "inline": False,
                },
            ],
            "footer": {"text": "Horizon prices are reconstructed from the stored short returns and signal entry price."},
        }

        cards: list[dict] = [summary]
        for risk_tier, title, color in (
            ("standard", "🟢 STANDARD • Signal Ledger", 0x57F287),
            ("high_risk", "🟡 HIGH RISK • Signal Ledger", 0xFEE75C),
            ("extreme_risk", "🔴 EXTREME RISK • Signal Ledger", 0xED4245),
        ):
            group = list(ledger.by_risk(risk_tier))
            if not group:
                continue
            page_size = 7
            total_pages = (len(group) + page_size - 1) // page_size
            for page_index in range(total_pages):
                page = group[page_index * page_size:(page_index + 1) * page_size]
                cards.append({
                    "title": title,
                    "description": f"Page **{page_index + 1}/{total_pages}** • {len(group)} traced signals",
                    "color": color,
                    "fields": [self._ledger_signal_field(item, tz) for item in page],
                })

        try:
            for index, embed in enumerate(cards):
                self._validate_discord_embed(embed)
                payload = {
                    "username": "Exhaustion Scanner • Ledger",
                    "embeds": [embed],
                    "allowed_mentions": {"parse": []},
                }
                if index == 0 and csv_bytes is not None:
                    filename = f"signal-outcome-ledger-{display_time.strftime('%Y-%m-%d')}.csv"
                    response = await self._client.post(
                        self._performance_webhook_url,
                        data={"payload_json": json.dumps(payload)},
                        files={"files[0]": (filename, csv_bytes, "text/csv")},
                    )
                else:
                    response = await self._client.post(self._performance_webhook_url, json=payload)
                if response.status_code >= 400:
                    LOGGER.error(
                        "Discord signal-ledger card %d/%d rejected status=%s body=%s",
                        index + 1,
                        len(cards),
                        response.status_code,
                        response.text[:2000],
                    )
                response.raise_for_status()
            return True
        except (httpx.HTTPError, ValueError):
            LOGGER.exception("Discord signal outcome ledger failed")
            return False

    def _ledger_signal_field(self, item: SignalLedgerItem, tz: ZoneInfo) -> dict:
        status_icon, status_label = self._ledger_status(item)
        target = (
            f"✅ **{self._hours(item.time_to_target_20_hours)}**"
            if item.target_20_at is not None
            else "⏳ not reached"
        )
        if item.target_20_at is not None:
            target += f" • {item.target_20_at.astimezone(tz).strftime('%d %b %H:%M')}"

        horizon_parts: list[str] = []
        for horizon in item.horizons:
            if horizon.return_pct is None:
                horizon_parts.append(f"**{horizon.label}** ⏳")
                continue
            icon = "🟢" if horizon.return_pct > 0 else "⚪"
            horizon_parts.append(
                f"**{horizon.label}** {icon} {self._price(horizon.price)} "
                f"({self._signed_percent(horizon.return_pct)})"
            )

        breach_parts: list[str] = []
        breach_icons = {100: "🔴", 200: "🟥", 300: "🟣", 400: "⚫"}
        for breach in item.breaches:
            if breach.occurred_at is None:
                breach_parts.append(f"-{breach.adverse_limit_pct}% 🟢 none")
            else:
                breach_parts.append(
                    f"-{breach.adverse_limit_pct}% {breach_icons[breach.adverse_limit_pct]} "
                    f"{self._hours(breach.hours_after_signal)}"
                )

        value = (
            f"**{status_icon} {status_label}**\n"
            f"Signal: `{item.confirmed_at.astimezone(tz).strftime('%d %b %H:%M')}` • "
            f"price **{self._price(item.signal_price)}** • episode #{item.episode_id}\n"
            f"🎯 +20%: {target}\n"
            f"{' • '.join(horizon_parts[:2])}\n"
            f"{' • '.join(horizon_parts[2:])}\n"
            f"Risk path: {' • '.join(breach_parts[:2])}\n"
            f"{' • '.join(breach_parts[2:])}"
        )
        return {
            "name": item.symbol,
            "value": value,
            "inline": False,
        }

    @staticmethod
    def _ledger_status(item: SignalLedgerItem) -> tuple[str, str]:
        status = item.headline_status
        if status == "target_hit":
            return "🟢", "TARGET HIT"
        if status == "target_then_breach":
            return "🟢→🔴", "TARGET HIT, LATER BREACH OBSERVED"
        if status == "breach_400":
            return "⚫", "CATASTROPHIC • -400% BREACH BEFORE +20%"
        if status == "breach_300":
            return "🟣", "SEVERE • -300% BREACH BEFORE +20%"
        if status == "breach_200":
            return "🟥", "SEVERE • -200% BREACH BEFORE +20%"
        if status == "breach_100":
            return "🔴", "LIQUIDATION-TYPE • -100% BREACH BEFORE +20%"
        if status == "profitable_below_target":
            return "🟡", "PROFITABLE • TARGET NOT YET HIT"
        if status == "safe_negative":
            return "⚪", "NEGATIVE • NO -100% BREACH OBSERVED"
        return "🔵", "PENDING"


    @staticmethod
    def _discord_embed_char_count(embed: dict) -> int:
        total = len(str(embed.get("title") or ""))
        total += len(str(embed.get("description") or ""))
        author = embed.get("author") or {}
        total += len(str(author.get("name") or ""))
        footer = embed.get("footer") or {}
        total += len(str(footer.get("text") or ""))
        for field in embed.get("fields") or []:
            total += len(str(field.get("name") or ""))
            total += len(str(field.get("value") or ""))
        return total

    @classmethod
    def _validate_discord_embed(cls, embed: dict) -> None:
        title = str(embed.get("title") or "")
        description = str(embed.get("description") or "")
        footer = str((embed.get("footer") or {}).get("text") or "")
        author = str((embed.get("author") or {}).get("name") or "")
        fields = embed.get("fields") or []
        if len(title) > 256:
            raise ValueError(f"Discord embed title too long: {len(title)}")
        if len(description) > 4096:
            raise ValueError(f"Discord embed description too long: {len(description)}")
        if len(fields) > 25:
            raise ValueError(f"Discord embed has too many fields: {len(fields)}")
        if len(footer) > 2048:
            raise ValueError(f"Discord embed footer too long: {len(footer)}")
        if len(author) > 256:
            raise ValueError(f"Discord embed author too long: {len(author)}")
        for field in fields:
            name = str(field.get("name") or "")
            value = str(field.get("value") or "")
            if len(name) > 256:
                raise ValueError(f"Discord embed field name too long: {len(name)}")
            if len(value) > 1024:
                raise ValueError(f"Discord embed field value too long: {len(value)}")
        total = cls._discord_embed_char_count(embed)
        if total > 6000:
            raise ValueError(f"Discord embed exceeds 6000-character budget: {total}")

    def _risk_embed(
        self,
        *,
        title: str,
        color: int,
        matrix: StrategyMatrixSummary,
        weekly: WeeklyRiskSummary,
    ) -> dict:
        fields: list[dict] = []

        for row in matrix.rows:
            fields.append({
                "name": self._strategy_row_title(row),
                "value": self._strategy_row_value(row),
                "inline": False,
            })

        fields.append({
            "name": "📅 Full 7-Day Path",
            "value": self._weekly_summary(weekly),
            "inline": False,
        })
        return {
            "title": title,
            "description": (
                f"**{matrix.total_signals}** traced signals • Strategy viability by maximum tolerated adverse move.\n"
                "A win means the strategy target was achieved **without first crossing** the selected loss threshold."
            ),
            "color": color,
            "fields": fields,
        }

    def _strategy_row_title(self, row: StrategyRowSummary) -> str:
        if row.strategy == "profit_20":
            return "🎯 +20% Profit Target • Horizon Independent"
        return f"⏱️ {row.label}"

    def _strategy_row_value(self, row: StrategyRowSummary) -> str:
        return "\n".join(self._strategy_threshold_line(row, cell) for cell in row.thresholds)

    def _strategy_threshold_line(
        self,
        row: StrategyRowSummary,
        cell: StrategyThresholdSummary,
    ) -> str:
        threshold = f"-{cell.adverse_limit_pct}%"
        icon = "✅" if cell.win_rate is not None and abs(cell.win_rate - 1.0) < 1e-12 else self._win_icon(cell.win_rate)

        if row.strategy == "profit_20":
            base = (
                f"{icon} **{threshold} max loss:** WR **{self._percent(cell.win_rate)}** • "
                f"wins {cell.wins}/{cell.resolved} resolved"
            )
            if cell.pending:
                base += f" • pending {cell.pending}"
            if cell.win_rate is not None and abs(cell.win_rate - 1.0) < 1e-12:
                base += (
                    f" • Avg **{self._signed_percent(cell.avg_profit)}** • "
                    f"Σ **{self._signed_percent(cell.sum_profit)}**"
                )
            if cell.avg_time_to_target_hours is not None:
                base += f" • avg t **{self._hours(cell.avg_time_to_target_hours)}**"
            return base

        horizon = row.label.replace(" profitable", "")
        base = (
            f"{icon} **{threshold} max loss:** WR **{self._percent(cell.win_rate)}** • "
            f"{cell.wins} wins • {cell.maturity_failures} not profitable at {horizon} • "
            f"{cell.breach_failures} breached"
        )
        if cell.win_rate is not None and abs(cell.win_rate - 1.0) < 1e-12:
            base += (
                f" • Avg **{self._signed_percent(cell.avg_profit)}** • "
                f"Σ **{self._signed_percent(cell.sum_profit)}**"
            )
        return base

    def _weekly_summary(self, summary: WeeklyRiskSummary) -> str:
        if summary.matured_7d == 0:
            return "⏳ No signals have completed the full 7-day observation window yet."
        return (
            f"**{summary.matured_7d}** fully matured signals\n"
            f"Ever profitable: **{self._percent(summary.ever_profitable_rate)}**\n"
            f"+100% adverse breach: **{self._percent(summary.isolated_100_breach_rate)}** • "
            f"before first profit: **{self._percent(summary.isolated_breach_before_profit_rate)}**\n"
            f"+400% adverse breach: **{self._percent(summary.cross_400_breach_rate)}** • "
            f"before first profit: **{self._percent(summary.cross_breach_before_profit_rate)}**"
        )

    def _raw_horizon_line(self, horizon: HorizonSummary) -> str:
        return (
            f"**{self._horizon_label(horizon.hours)}**  {self._win_icon(horizon.win_rate)} "
            f"WR **{self._percent(horizon.win_rate)}**  •  Avg **{self._signed_percent(horizon.avg_return)}**  •  "
            f"Σ **{self._signed_percent(horizon.sum_return)}**  •  n={horizon.matured_total}"
        )

    @staticmethod
    def _horizons(report: PerformanceSummary) -> tuple[HorizonSummary, ...]:
        return (
            report.horizon_24h,
            report.horizon_48h,
            report.horizon_72h,
            report.horizon_168h,
        )

    @staticmethod
    def _horizon_label(hours: int) -> str:
        return "7D" if hours == 168 else f"{hours // 24}D"

    @staticmethod
    def _pretty_label(label: str) -> str:
        return label.replace("SHADOW PERFORMANCE", "PERFORMANCE").title()

    @staticmethod
    def _win_icon(value: float | None) -> str:
        if value is None:
            return "⚪"
        if value >= 0.70:
            return "🟢"
        if value >= 0.50:
            return "🟡"
        return "🔴"

    @staticmethod
    def _percent(value: object) -> str:
        return "n/a" if value is None else f"{float(value):.2%}"

    @staticmethod
    def _signed_percent(value: object) -> str:
        if value is None:
            return "n/a"
        return f"{float(value):+.2%}"

    @staticmethod
    def _hours(value: object) -> str:
        if value is None:
            return "n/a"
        hours = float(value)
        if hours < 24:
            return f"{hours:.1f}h"
        return f"{hours / 24.0:.2f}d"

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

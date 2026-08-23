from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.models import RunSignal
from app.signal_ledger import SignalLedger, SignalLedgerItem
from app.signal_ledger_table import LedgerTableImage
from app.research_analytics import (
    ResearchAnalyticsReport,
    FeatureSliceSummary,
    ExitHorizonSummary,
    TP5_CHALLENGER_MAX_SLOTS,
)
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
                "Confirmed-short signals only • STANDARD + HIGH only • Positive return = profitable short"
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
                    "name": "🎯 Raw Results — STANDARD + HIGH",
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
            title="🟢 STANDARD Signal Outcomes",
            color=0x57F287,
            matrix=report.standard_strategy_matrix,
            weekly=report.standard_weekly,
        )
        high = self._risk_embed(
            title="🟡 HIGH RISK Signal Outcomes",
            color=0xFEE75C,
            matrix=report.high_strategy_matrix,
            weekly=report.high_weekly,
        )
        methodology = {
            "title": "🧭 How to Read the Signal Outcomes",
            "description": (
                "This board describes historical signal behavior. It does **not** prescribe a stop-loss, "
                "holding period, leverage, position size, or portfolio strategy."
            ),
            "color": 0x99AAB5,
            "fields": [
                {
                    "name": "🎯 +20% target race",
                    "value": (
                        "For each adverse threshold, **Target-first rate** uses only resolved target-vs-breach "
                        "outcomes. Pending signals stay pending. Same-15m-candle target/breach is conservatively "
                        "breach-first. Average time is measured only for target-first observations."
                    ),
                    "inline": False,
                },
                {
                    "name": "⏱️ 1D / 2D / 3D / 7D outcomes",
                    "value": (
                        "Every signal that reached the exact horizon contributes its raw short return to **Avg raw** "
                        "and **Σ raw** — including negative returns and signals that crossed adverse thresholds. "
                        "Profitable rate simply means return > 0 at that horizon."
                    ),
                    "inline": False,
                },
                {
                    "name": "💥 Adverse thresholds",
                    "value": (
                        "**-100%** = price reaches 2× entry • **-200%** = 3× • **-300%** = 4× • **-400%** = 5×. "
                        "Counts show whether that excursion occurred before the horizon; they are path observations, "
                        "not assumed exits."
                    ),
                    "inline": False,
                },
                {
                    "name": "Σ raw returns",
                    "value": (
                        "Arithmetic sum of the matured signal returns at that horizon. It is **not portfolio return**, "
                        "because signals may overlap and position sizing is not modeled."
                    ),
                    "inline": False,
                },
                {
                    "name": "Audience scope",
                    "value": "Only **STANDARD** and **HIGH RISK** signals are included in the public performance and ledger datasets.",
                    "inline": False,
                },
            ],
            "footer": {"text": "No fees, slippage, funding, leverage or overlapping-position portfolio effects included."},
        }

        # Each card is sent separately so every embed has its own Discord text budget.
        embeds = (overview, standard, high, methodology)
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
        table_images: tuple[LedgerTableImage, ...] | None = None,
        as_of: datetime | None = None,
        timezone_name: str = "Europe/Zurich",
    ) -> bool:
        """Send the compact subscriber-facing signal outcome ledger.

        The detailed raw data is attached as CSV. Discord itself receives a compact
        summary card followed by PNG table pages, split by execution-risk tier.
        """
        if not self._performance_webhook_url:
            return False

        tz = ZoneInfo(timezone_name)
        display_time = (as_of or ledger.generated_at).astimezone(tz)
        risk_counts = {
            "standard": len(ledger.by_risk("standard")),
            "high_risk": len(ledger.by_risk("high_risk")),
        }
        target5_before_100 = sum(item.target_5_before_100_breach is True for item in ledger.items)
        target5_hits = sum(item.target_5_at is not None for item in ledger.items)
        target5_mae = [
            -item.path_mae_before_target_5
            for item in ledger.items
            if item.path_mae_before_target_5 is not None
        ]
        target_before_100 = sum(item.target_before_100_breach is True for item in ledger.items)
        breach_before_target = sum(
            item.target_before_100_breach is False and item.first_100_breach_at is not None
            for item in ledger.items
        )
        pending_target_race = ledger.total - target_before_100 - breach_before_target

        summary = {
            "title": "📒 Exhaustion Scanner • Signal Outcome Table",
            "description": (
                f"Updated **{display_time.strftime('%d %b %Y • %H:%M %Z')}**\n"
                "Compact visual tables below • newest signals first • full raw ledger attached as CSV"
            ),
            "color": 0x5865F2,
            "fields": [
                {
                    "name": "📦 Signals",
                    "value": (
                        f"**{ledger.total}** total • "
                        f"🟢 STANDARD **{risk_counts['standard']}** • "
                        f"🟡 HIGH **{risk_counts['high_risk']}**"
                    ),
                    "inline": False,
                },
                {
                    "name": "⚡ TP5 research race",
                    "value": (
                        f"+5% hit **{target5_hits}/{ledger.total}** • "
                        f"before -100% **{target5_before_100}**"
                        + (
                            f" • median pre-hit adverse **{self._percent(statistics.median(target5_mae))}**"
                            if target5_mae else ""
                        )
                    ),
                    "inline": False,
                },
                {
                    "name": "🎯 +20% vs -100%",
                    "value": (
                        f"Target first **{target_before_100}** • "
                        f"breach first **{breach_before_target}** • "
                        f"pending **{pending_target_race}**"
                    ),
                    "inline": False,
                },
                {
                    "name": "💥 Observed adverse breaches",
                    "value": (
                        f"-100% **{ledger.count_breach(100)}** • "
                        f"-200% **{ledger.count_breach(200)}** • "
                        f"-300% **{ledger.count_breach(300)}** • "
                        f"-400% **{ledger.count_breach(400)}**"
                    ),
                    "inline": False,
                },
                {
                    "name": "Table colors",
                    "value": (
                        "🟢 profitable/target • 🟠 negative but no -100% breach • "
                        "🔴 liquidation-type breach • 🔵 pending"
                    ),
                    "inline": False,
                },
            ],
        }

        try:
            self._validate_discord_embed(summary)
            summary_payload = {
                "username": "Exhaustion Scanner • Ledger",
                "embeds": [summary],
                "allowed_mentions": {"parse": []},
            }
            if csv_bytes is not None:
                filename = f"signal-outcome-ledger-{display_time.strftime('%Y-%m-%d')}.csv"
                response = await self._client.post(
                    self._performance_webhook_url,
                    data={"payload_json": json.dumps(summary_payload)},
                    files={"files[0]": (filename, csv_bytes, "text/csv")},
                )
            else:
                response = await self._client.post(self._performance_webhook_url, json=summary_payload)
            if response.status_code >= 400:
                LOGGER.error(
                    "Discord signal-ledger summary rejected status=%s body=%s",
                    response.status_code,
                    response.text[:2000],
                )
            response.raise_for_status()

            for index, table in enumerate(table_images or (), start=1):
                embed = {
                    "title": f"{table.risk_label} • Signal Outcomes",
                    "description": f"Page **{table.page}/{table.total_pages}** • exact values available in CSV",
                    "color": {
                        "standard": 0x57F287,
                        "high_risk": 0xFEE75C,
                    }.get(table.risk_tier, 0x5865F2),
                    "image": {"url": f"attachment://{table.filename}"},
                }
                self._validate_discord_embed(embed)
                payload = {
                    "username": "Exhaustion Scanner • Ledger",
                    "embeds": [embed],
                    "allowed_mentions": {"parse": []},
                }
                response = await self._client.post(
                    self._performance_webhook_url,
                    data={"payload_json": json.dumps(payload)},
                    files={"files[0]": (table.filename, table.png_bytes, "image/png")},
                )
                if response.status_code >= 400:
                    LOGGER.error(
                        "Discord signal-ledger table %d/%d rejected status=%s body=%s",
                        index,
                        len(table_images or ()),
                        response.status_code,
                        response.text[:2000],
                    )
                response.raise_for_status()
            return True
        except (httpx.HTTPError, ValueError):
            LOGGER.exception("Discord signal outcome ledger failed")
            return False

    async def send_research_analytics(
        self,
        report: ResearchAnalyticsReport,
        *,
        feature_csv: bytes | None = None,
        dataset_csv: bytes | None = None,
        strategy_csv: bytes | None = None,
        entry_csv: bytes | None = None,
        regime_csv: bytes | None = None,
        as_of: datetime | None = None,
        timezone_name: str = "Europe/Zurich",
    ) -> bool:
        """Send an on-demand research-only strategy diagnostic board."""
        if not self._performance_webhook_url:
            return False

        tz = ZoneInfo(timezone_name)
        display_time = (as_of or report.generated_at).astimezone(tz)
        b = report.baseline
        completeness_7d = (b.complete_paths_7d / b.matured_7d) if b.matured_7d else None
        completeness_14d = (b.complete_paths_14d / b.total_signals) if b.total_signals else None

        target_lines = [
            f"**+{target.target_pct}%** hit **{self._percent(target.hit_rate)}** "
            f"({target.hits}/{target.sample}) • median **{self._hours(target.median_time_hours)}** "
            f"• p75 **{self._hours(target.p75_time_hours)}**"
            for target in report.target_sweep
        ]

        tp5 = report.tp5_risk
        tp5_race_lines = [
            f"**-{item.adverse_threshold_pct}%**: TP5 first **{item.target_first}/{item.sample}** "
            f"({self._percent(item.target_first_rate)}) • adverse first **{item.adverse_first}**"
            + (f" • same 15m candle **{item.same_candle}**" if item.same_candle else "")
            + (f" • unresolved **{item.unresolved}**" if item.unresolved else "")
            for item in tp5.adverse_races
        ]

        def portfolio_line(item) -> str:
            gate_text = (
                f" • gate eligible **{item.eligible_signals}/{item.signals}**"
                if item.filtered_entry_gate else ""
            )
            strategy_filter_text = (
                f" • behavior filtered **{item.filtered_strategy}**"
                if getattr(item, "filtered_strategy", 0) else ""
            )
            mtm = (
                f" • MTM DD **-{self._percent(item.max_mtm_drawdown)}**"
                if item.max_mtm_drawdown is not None else " • MTM DD n/a"
            )
            ratio = (
                f" • R/DD **{item.return_over_max_drawdown:.2f}x**"
                if item.return_over_max_drawdown is not None else ""
            )
            return (
                f"**{item.strategy}** • signals {item.signals}{gate_text}{strategy_filter_text} • entered **{item.entered}** • "
                f"return **{self._signed_percent(item.marked_return)}**{mtm}{ratio}\n"
                f"max slots **{item.max_open_positions}** • max exp **{self._percent(item.max_observed_exposure_pct)}** • "
                f"avg/p95 exp {self._percent(item.avg_exposure_pct)}/{self._percent(item.p95_exposure_pct)} • "
                f"max losers **{item.max_simultaneous_losers}**\n"
                f"capacity miss {item.missed_capacity} • same-symbol {item.missed_same_symbol} • "
                f"median hold **{self._hours(item.median_holding_hours)}** • slot-days **{item.slot_days:.2f}** • "
                f"return/slot-day **{self._signed_percent(item.return_per_slot_day)}**"
            )

        regime = report.token_regime
        behavior_lines = []
        for item in regime.buckets:
            behavior_lines.append(
                f"**{item.behavior_class}** • n={item.signals} • TP5 "
                f"{item.tp5_hits}/{item.signals} ({self._percent(item.tp5_hit_rate)}) • "
                f"median/p75 {self._hours(item.median_tp5_hours)}/{self._hours(item.p75_tp5_hours)} • "
                f"MAE med/p75 {self._percent(item.median_pre_tp5_adverse)}/{self._percent(item.p75_pre_tp5_adverse)}"
            )
        def regime_portfolio_line(item) -> str:
            return (
                f"**{item.strategy}** • entered {item.entered}/{item.signals} • "
                f"return **{self._signed_percent(item.marked_return)}** • "
                f"DD **-{self._percent(item.max_mtm_drawdown)}** • "
                f"cap miss {item.missed_capacity} • median hold {self._hours(item.median_holding_hours)}"
            )

        def regime_efficiency_line(item) -> str:
            calendar_days = report.calendar_throughput.history_span_days
            releases_per_day = (item.closed / calendar_days) if calendar_days > 0 else None
            total_slot_days = TP5_CHALLENGER_MAX_SLOTS * calendar_days
            used_capacity = (item.slot_days / total_slot_days) if total_slot_days > 0 else None
            idle_capacity = (max(0.0, min(1.0, 1.0 - used_capacity)) if used_capacity is not None else None)
            return (
                f"**{item.strategy}** • slot-days **{item.slot_days:.2f}** • "
                f"return/slot-day **{self._signed_percent(item.return_per_slot_day)}** • "
                f"releases/day **{releases_per_day:.2f}** • idle capacity **{self._percent(idle_capacity)}** • "
                f"avg/p95 exposure **{self._percent(item.avg_exposure_pct)}/{self._percent(item.p95_exposure_pct)}**"
            )

        regime_portfolio_items = [report.calendar_throughput.tp5, *report.regime_portfolios]
        regime_portfolio_lines = [regime_portfolio_line(item) for item in regime_portfolio_items]
        regime_efficiency_lines = [regime_efficiency_line(item) for item in regime_portfolio_items]

        def slice_lines(items: tuple[FeatureSliceSummary, ...], icon: str) -> str:
            if not items:
                return "Not enough matured observations yet."
            return "\n".join(
                f"{icon} **{item.feature_label} — {item.bucket}** • n={item.sample} • "
                f"target lift {self._signed_percent(item.target_lift_pp)} • "
                f"7d positive lift {self._signed_percent(item.positive_lift_pp)} • "
                f"avg-return lift {self._signed_percent(item.avg_return_lift_pp)}"
                for item in items
            )

        def standard_exit_line(item: ExitHorizonSummary) -> str:
            return (
                f"**{item.horizon_hours // 24}d** n={item.sample} • avg **{self._signed_percent(item.avg_return)}** • "
                f"median **{self._signed_percent(item.median_return)}** • positive **{self._percent(item.positive_rate)}** • "
                f"avg/day **{self._signed_percent(item.avg_return_per_day)}**"
            )

        standard_early_lines = [
            standard_exit_line(item)
            for item in report.standard_exit_sweep
            if item.cohort_horizon_hours == 168 and item.sample
        ]
        standard_extended_lines = [
            standard_exit_line(item)
            for item in report.standard_exit_sweep
            if item.cohort_horizon_hours == 336 and item.sample
        ]
        risky_lines = [
            (
                f"**TP20 or {item.timeout_hours // 24}d** n={item.sample} • "
                + (
                    f"TP **{self._percent(item.target_hit_rate)}** • avg **{self._signed_percent(item.avg_strategy_return)}** • "
                    f"worst **{self._signed_percent(item.worst_strategy_return)}** • hold **{self._hours(item.avg_holding_hours)}** • "
                    f"slot/day **{self._signed_percent(item.return_per_slot_day)}**"
                    if item.sample
                    else "cohort not mature / complete yet"
                )
            )
            for item in report.high_risk_timeout_sweep
        ]
        all_stop = [item for item in report.stop_survival if item.risk_tier == "all"]
        stop_lines = [
            f"**-{item.stop_pct}%** would kill **{item.winners_killed}/{item.winners_with_path}** eventual +20% winners "
            f"({self._percent(item.kill_rate)})"
            for item in all_stop
        ]
        score_lines = [
            f"**{item.score_name.replace('_', ' ').title()} {item.bucket}** • n={item.sample} • "
            f"TP20 {self._percent(item.target_20_rate_7d)} • 7d+ {self._percent(item.positive_7d_rate)} • "
            f"avg {self._signed_percent(item.avg_return_7d)}"
            for item in report.score_buckets
        ]
        top_interactions = report.ranked_interactions[:4]
        interaction_lines = [
            f"🧬 **{item.interaction}** — {item.bucket} • n={item.sample} • "
            f"rank lift {self._signed_percent(item.rank_score)}"
            for item in top_interactions
        ]
        delayed_lines = [
            f"**+{item.delay_minutes}m** n={item.sample} • TP20 {self._percent(item.target_20_rate_7d)} • "
            f"avg7d {self._signed_percent(item.avg_return_7d)} • median adverse -{self._percent(item.median_adverse_7d)}"
            for item in report.delayed_entries
            if item.sample
        ]
        cohort_lines = [
            f"**{item.cohort.replace('_', ' ').title()}** • signals **{item.signals}** • complete7d **{item.complete_7d}** • "
            f"TP5 **{item.tp5_hits}/{item.complete_7d}** ({self._percent(item.tp5_hit_rate)}) • "
            f"gate eligible **{item.entrygate_eligible}/{item.signals}** ({self._percent(item.entrygate_eligible_rate)})"
            + (f" • worst pre-TP5 **-{self._percent(item.worst_pre_tp5_adverse)}**" if item.complete_7d else "")
            for item in report.prospective_cohorts
        ]
        post_freeze_score_lines = [
            f"**{item.score_name.replace('_', ' ').title()} {item.bucket}** • n={item.sample} • "
            f"TP20 {self._percent(item.target_20_rate_7d)} • 7d+ {self._percent(item.positive_7d_rate)} • "
            f"avg {self._signed_percent(item.avg_return_7d)}"
            for item in report.prospective_score_buckets
            if item.cohort == "post_freeze" and item.sample
        ]

        live_tp5 = report.prospective_tp5_live
        gate_monitor = report.prospective_gate_acceptance

        def regime_value(item, value) -> str:
            if value is None:
                return "n/a"
            if item.feature == "amount_24h":
                return f"{value / 1_000_000:.2f}m"
            if item.feature == "return_24h":
                return self._signed_percent(value)
            return f"{value:.2f}"

        regime_lines = [
            (
                f"**{item.feature_label}** • med {regime_value(item, item.discovery_median)} → "
                f"{regime_value(item, item.post_freeze_median)} • "
                f"post below/IQR/above **{self._percent(item.post_below_discovery_p25_rate)} / "
                f"{self._percent(item.post_inside_discovery_iqr_rate)} / "
                f"{self._percent(item.post_above_discovery_p75_rate)}**"
            )
            for item in report.prospective_regime_drift
            if item.post_freeze_sample
        ]

        portfolio_names = {
            "current_live_5standard_1high": "Current",
            "tp5_challenger_6x5pct": "TP5",
            "entrygate_v1__current_live_5standard_1high": "Gate + current",
            "entrygate_v1__tp5_challenger_6x5pct": "Gate + TP5",
        }
        prospective_portfolio_lines = [
            (
                f"**{portfolio_names.get(item.strategy, item.strategy)}** • entered {item.entered}/{item.signals} • "
                f"return {self._signed_percent(item.marked_return)} • DD "
                f"{'-' + self._percent(item.max_mtm_drawdown) if item.max_mtm_drawdown is not None else 'n/a'} • "
                f"R/DD {f'{item.return_over_max_drawdown:.2f}x' if item.return_over_max_drawdown is not None else 'n/a'}"
            )
            for item in report.prospective_portfolios
            if item.signals
        ]

        calendar = report.calendar_throughput

        def throughput_line(item) -> str:
            utilization = (item.entered / item.signals) if item.signals else None
            entries_per_day = (item.entered / calendar.history_span_days) if calendar.history_span_days > 0 else None
            releases_per_day = (item.closed / calendar.history_span_days) if calendar.history_span_days > 0 else None
            return (
                f"**{portfolio_names.get(item.strategy, item.strategy)}** • entered **{item.entered}/{item.signals}** "
                f"({self._percent(utilization)}) • closed **{item.closed}** • open **{item.open_positions}**\n"
                f"capacity miss **{item.missed_capacity}** • same-symbol **{item.missed_same_symbol}** • "
                f"entries/day **{entries_per_day:.2f}** • releases/day **{releases_per_day:.2f}**\n"
                f"return **{self._signed_percent(item.marked_return)}** • MTM DD "
                f"**{'-' + self._percent(item.max_mtm_drawdown) if item.max_mtm_drawdown is not None else 'n/a'}** • "
                f"median hold **{self._hours(item.median_holding_hours)}**"
            )

        if calendar.latest_30d_current is not None and calendar.latest_30d_tp5 is not None:
            monthly_lines = [
                (
                    f"**{portfolio_names.get(item.strategy, item.strategy)}** • signals {item.signals} • "
                    f"entered **{item.entered}** • utilization **{self._percent(item.entered / item.signals if item.signals else None)}** • "
                    f"30d return **{self._signed_percent(item.marked_return)}** • DD "
                    f"**{'-' + self._percent(item.max_mtm_drawdown) if item.max_mtm_drawdown is not None else 'n/a'}**"
                )
                for item in (calendar.latest_30d_current, calendar.latest_30d_tp5)
            ]
            monthly_value = "\n".join(monthly_lines)
        else:
            monthly_value = (
                f"Not available yet: observed history **{calendar.history_span_days:.2f}d**; "
                f"need **{calendar.days_until_30d:.2f}d** more for the first true 30-day empty-book replay. "
                "No short-window return is extrapolated."
            )

        overview = {
            "title": "🔬 Exhaustion Scanner • Research Analytics",
            "description": (
                f"Updated **{display_time.strftime('%d %b %Y • %H:%M %Z')}**\n"
                "Research-only diagnostics from frozen signal features + stored 15m post-signal paths."
            ),
            "color": 0x5865F2,
            "fields": [
                {
                    "name": "📦 Sample & path completeness",
                    "value": (
                        f"Signals **{b.total_signals}** • 7d matured **{b.matured_7d}**\n"
                        f"Complete 7d paths **{b.complete_paths_7d}** ({self._percent(completeness_7d)}) • "
                        f"complete 14d paths **{b.complete_paths_14d}** ({self._percent(completeness_14d)})\n"
                        f"Feature ranking minimum **n={report.min_rank_sample}**."
                    ),
                    "inline": False,
                },
                {
                    "name": "🎯 Current 7d baseline",
                    "value": (
                        f"+20% **{self._percent(b.target_20_rate_7d)}** • positive **{self._percent(b.positive_7d_rate)}**\n"
                        f"Avg **{self._signed_percent(b.avg_return_7d)}** • median **{self._signed_percent(b.median_return_7d)}** • "
                        f"median time to +20% **{self._hours(b.median_time_to_20_hours)}**"
                    ),
                    "inline": False,
                },
                {
                    "name": "🌊 Fully observed 7d path",
                    "value": (
                        f"Median MFE **{self._signed_percent(b.median_mfe_7d)}** • adverse **-{self._percent(b.median_adverse_7d)}**\n"
                        f"Adverse before +20% **-{self._percent(b.median_adverse_before_20)}** • "
                        f"MFE timing **{self._hours(b.median_time_to_mfe_hours)}** • worst timing **{self._hours(b.median_time_to_mae_hours)}**"
                    ),
                    "inline": False,
                },
            ],
            "footer": {"text": "Shadow research only: no scanner or trader rule is changed by this report."},
        }
        targets = {
            "title": "🎚️ Profit-Target Sweep • 7-Day Paths",
            "description": "Favorable excursions from the original confirmed-short entry; complete 7-day paths only.",
            "color": 0x57F287,
            "fields": [{"name": "Target hit rate & speed", "value": "\n".join(target_lines) or "No complete paths yet.", "inline": False}],
        }
        tp5_challenger = {
            "title": "⚡ TP5 Challenger • Frozen Shadow Portfolio",
            "description": (
                "Research only. Paired complete-7d signal cohort. Challenger is frozen at "
                "6 generic slots × 5% equity, 30% cap, 1×, immediate entry, full +5% exit, "
                "one open position per symbol. Fees: 0.08% per fill."
            ),
            "color": 0x2ECC71,
            "fields": [
                {
                    "name": "Pre-TP5 path risk",
                    "value": (
                        f"+5% hit **{tp5.hits}/{tp5.sample}** ({self._percent(tp5.hit_rate)}) • "
                        f"median **{self._hours(tp5.median_time_hours)}** • p75 **{self._hours(tp5.p75_time_hours)}**\n"
                        f"Pre-hit adverse: median **-{self._percent(tp5.median_adverse_before_target)}** • "
                        f"p75 **-{self._percent(tp5.p75_adverse_before_target)}** • "
                        f"worst **-{self._percent(tp5.worst_adverse_before_target)}**"
                    ),
                    "inline": False,
                },
                {
                    "name": "+5% race vs adverse move",
                    "value": "\n".join(tp5_race_lines) or "No complete paths yet.",
                    "inline": False,
                },
                {
                    "name": "15m close-marked portfolio risk",
                    "value": (
                        f"Max MTM drawdown **-{self._percent(report.portfolio_tp5.max_mtm_drawdown)}** • "
                        f"worst equity **{self._signed_percent(report.portfolio_tp5.worst_mtm_return)}** • "
                        f"max unrealized loss **-{self._percent(report.portfolio_tp5.max_unrealized_loss)}**\n"
                        f"Avg/p95 exposure **{self._percent(report.portfolio_tp5.avg_exposure_pct)} / "
                        f"{self._percent(report.portfolio_tp5.p95_exposure_pct)}** • "
                        f"avg slots **{report.portfolio_tp5.avg_open_positions:.2f}** • "
                        f"max losers **{report.portfolio_tp5.max_simultaneous_losers}** • "
                        f"recovery **{self._hours(report.portfolio_tp5.drawdown_recovery_hours)}** "
                        if report.portfolio_tp5.avg_open_positions is not None else "MTM path marks unavailable."
                    ),
                    "inline": False,
                },
            ],
            "footer": {
                "text": "No live trader rules are changed. Same-candle TP/adverse races are intentionally reported as ambiguous."
            },
        }
        strategy_lab = {
            "title": "🏁 Champion vs challenger • Four-Way Paired Portfolio",
            "description": (
                "Four frozen research portfolios on the same complete-7d cohort. EntryGate-v1 accepts "
                "Entry Quality ≥4 and Continuation Risk ≤6. Live execution is unchanged."
            ),
            "color": 0xE67E22,
            "fields": [
                {
                    "name": "Champion vs TP5",
                    "value": "\n\n".join([
                        portfolio_line(report.portfolio_current),
                        portfolio_line(report.portfolio_tp5),
                    ]),
                    "inline": False,
                },
                {
                    "name": "EntryGate-v1 challengers",
                    "value": "\n\n".join([
                        portfolio_line(report.portfolio_entrygate_current),
                        portfolio_line(report.portfolio_entrygate_tp5),
                    ]),
                    "inline": False,
                },
                {
                    "name": "Prospective freeze • discovery vs post-freeze",
                    "value": (
                        f"Frozen **{report.oos_freeze_at.astimezone(tz).strftime('%d %b %Y • %H:%M %Z')}**; "
                        "cohort assignment uses signal confirmation time.\n" +
                        "\n".join(cohort_lines)
                    ),
                    "inline": False,
                },
                {
                    "name": "Post-freeze score buckets",
                    "value": "\n".join(post_freeze_score_lines) or "No post-freeze complete-7d observations yet.",
                    "inline": False,
                },
            ],
            "footer": {
                "text": "EntryGate-v1 and TP5 remain shadow-only. Do not retune frozen thresholds on the prospective cohort."
            },
        }
        prospective_monitor = {
            "title": "📡 Prospective Monitor • Post-Freeze",
            "description": (
                "Fast shadow monitoring after the frozen 21 Aug 2026 research cutoff. TP5 hits can resolve "
                "before 7d maturity; paired portfolio comparisons still require complete 7d paths."
            ),
            "color": 0x1ABC9C,
            "fields": [
                {
                    "name": "⚡ TP5 live tracker • no 7d wait",
                    "value": (
                        f"Signals **{live_tp5.signals}** • hit **{live_tp5.hits}** • waiting **{live_tp5.waiting}** • "
                        f"failed after complete 7d **{live_tp5.failed}**\n"
                        f"Resolved hit rate **{self._percent(live_tp5.resolved_hit_rate)}** • "
                        f"median/p75 hit **{self._hours(live_tp5.median_hit_hours)} / {self._hours(live_tp5.p75_hit_hours)}**\n"
                        f"Worst pre-hit adverse **-{self._percent(live_tp5.worst_pre_hit_adverse)}** • "
                        f"worst waiting close-mark adverse **-{self._percent(live_tp5.worst_waiting_close_adverse)}** • "
                        f"oldest waiting **{self._hours(live_tp5.oldest_waiting_hours)}**"
                    ),
                    "inline": False,
                },
                {
                    "name": "🚦 EntryGate-v1 acceptance",
                    "value": (
                        f"Post-freeze eligible **{gate_monitor.eligible}/{gate_monitor.signals}** "
                        f"({self._percent(gate_monitor.eligible_rate)}) • "
                        f"latest {gate_monitor.rolling_signals}/{gate_monitor.rolling_window} signals: "
                        f"**{gate_monitor.rolling_eligible}** eligible "
                        f"({self._percent(gate_monitor.rolling_eligible_rate)})"
                    ),
                    "inline": False,
                },
                {
                    "name": "🌡️ Regime drift • discovery median → post-freeze",
                    "value": "\n".join(regime_lines) or "No post-freeze signals yet; frozen discovery distributions remain the reference.",
                    "inline": False,
                },
                {
                    "name": "🏁 Post-freeze paired portfolios • complete 7d only",
                    "value": "\n".join(prospective_portfolio_lines) or "No post-freeze complete-7d observations yet.",
                    "inline": False,
                },
            ],
            "footer": {
                "text": "Fast TP5 monitoring is descriptive; strategy selection remains based on frozen prospective comparisons."
            },
        }
        calendar_throughput = {
            "title": "♻️ Calendar Throughput • Current vs TP5",
            "description": (
                "Same incoming signal stream, chronological slot occupancy, fees and equity recycling. "
                "Unlike the paired 7d board, this includes immature/open signals so burst congestion is visible."
            ),
            "color": 0xF1C40F,
            "fields": [
                {
                    "name": f"Observed calendar window • {calendar.history_span_days:.2f}d",
                    "value": "\n\n".join([
                        throughput_line(calendar.current),
                        throughput_line(calendar.tp5),
                    ]) if calendar.current.signals else "No observed signals yet.",
                    "inline": False,
                },
                {
                    "name": "📅 Latest true 30-day replay",
                    "value": monthly_value,
                    "inline": False,
                },
            ],
            "footer": {
                "text": "30d windows start with equal equity and an empty book; report actual calendar return, never short-window annualization."
            },
        }
        token_behavior = {
            "title": "🧬 Token Behaviour • Regime Dependency",
            "description": (
                f"Research only. Uses {regime.lookback_days}d of completed pre-signal 4h token/BTC history. "
                "Episodicness combines low BTC explanatory power, concentration in a few positive spikes, "
                "and isolated +5% 4h pumps outperforming BTC. Cutoffs are calibrated on the frozen discovery cohort only."
            ),
            "color": 0x3498DB,
            "fields": [
                {
                    "name": "History coverage",
                    "value": (
                        f"Profiled **{regime.profiled_signals}** • insufficient **{regime.insufficient_signals}** • "
                        f"discovery reference **{regime.discovery_profiled_signals}** • "
                        f"minimum paired 4h returns **{regime.minimum_paired_returns}**"
                    ),
                    "inline": False,
                },
                {
                    "name": "Behaviour buckets • TP5 path",
                    "value": "\n".join(behavior_lines) or "History backfill/profile coverage is not ready yet.",
                    "inline": False,
                },
                {
                    "name": "TP5-6 shadow portfolios • same calendar stream",
                    "value": "\n\n".join(regime_portfolio_lines),
                    "inline": False,
                },
                {
                    "name": "Capital-time efficiency • same calendar stream",
                    "value": "\n".join(regime_efficiency_lines),
                    "inline": False,
                },
            ],
            "footer": {
                "text": "No scanner or trader rule is changed. Idle = unused share of 6 TP5 slots over observed calendar time; priority only reorders same-timestamp signals."
            },
        }

        exits = {
            "title": "🧭 Exit Research • Standard vs High Risk",
            "description": "Paired-cohort exits: Standard 1–7d uses one complete-7d cohort; High Risk 1–10d uses one fully mature/evaluable 10d cohort. 14d remains separate until mature.",
            "color": 0x3498DB,
            "fields": [
                {"name": "STANDARD paired cohort • 1–7d", "value": "\n".join(standard_early_lines) or "Complete paired 7d cohort is still maturing.", "inline": False},
                {"name": "STANDARD paired cohort • 8–14d", "value": "\n".join(standard_extended_lines) or "Complete paired 14d cohort is still maturing.", "inline": False},
                {"name": "HIGH RISK • paired TP20 + timeout", "value": "\n".join(risky_lines) or "No fully mature timeout cohorts yet.", "inline": False},
            ],
        }
        survival = {
            "title": "🛡️ Winner Survival • Hypothetical Stops",
            "description": "Among observed signals that eventually reached +20% within 7d, how many would an adverse stop have killed first?",
            "color": 0xED4245,
            "fields": [{"name": "All public tiers", "value": "\n".join(stop_lines) or "Not enough path data yet.", "inline": False}],
        }
        entry = {
            "title": "🧠 Entry Research • Shadow Only",
            "description": "Candidate quality/continuation scores are frozen hypotheses; interactions and delayed entries are exploratory.",
            "color": 0x9B59B6,
            "fields": [
                {"name": "Shadow score buckets", "value": "\n".join(score_lines) or "No matured observations yet.", "inline": False},
                {"name": "Strongest interactions", "value": "\n".join(interaction_lines) or "Not enough interaction sample yet.", "inline": False},
                {"name": "Delayed-entry simulation • paired cohort", "value": "\n".join(delayed_lines) or "Common complete delayed-entry cohort is still maturing.", "inline": False},
            ],
        }
        features = {
            "title": "🧪 Feature Lift • Candidate Filters",
            "description": "Univariate tertile/boolean slices versus the all-signal 7d baseline; exploratory, not causal.",
            "color": 0xFEE75C,
            "fields": [
                {"name": "⬆️ Strongest observed slices", "value": slice_lines(report.best_slices, "🟢"), "inline": False},
                {"name": "⬇️ Weakest observed slices", "value": slice_lines(report.worst_slices, "🔴"), "inline": False},
            ],
        }

        embeds = (overview, targets, tp5_challenger, strategy_lab, prospective_monitor, calendar_throughput, token_behavior, exits, survival, entry, features)
        try:
            for embed in embeds:
                self._validate_discord_embed(embed)
            payload = {
                "username": "Exhaustion Scanner • Research",
                "embeds": [overview],
                "allowed_mentions": {"parse": []},
            }
            files: dict[str, tuple[str, bytes, str]] = {}
            attachments = [
                (feature_csv, f"research-feature-lift-{display_time.strftime('%Y-%m-%d')}.csv"),
                (dataset_csv, f"research-signal-dataset-{display_time.strftime('%Y-%m-%d')}.csv"),
                (strategy_csv, f"research-strategy-sweeps-{display_time.strftime('%Y-%m-%d')}.csv"),
                (entry_csv, f"research-entry-analysis-{display_time.strftime('%Y-%m-%d')}.csv"),
                (regime_csv, f"research-token-regime-{display_time.strftime('%Y-%m-%d')}.csv"),
            ]
            for index, (content, filename) in enumerate(attachments):
                if content is not None:
                    files[f"files[{index}]"] = (filename, content, "text/csv")
            if files:
                response = await self._client.post(
                    self._performance_webhook_url,
                    data={"payload_json": json.dumps(payload)},
                    files=files,
                )
            else:
                response = await self._client.post(self._performance_webhook_url, json=payload)
            response.raise_for_status()

            for embed in embeds[1:]:
                response = await self._client.post(
                    self._performance_webhook_url,
                    json={
                        "username": "Exhaustion Scanner • Research",
                        "embeds": [embed],
                        "allowed_mentions": {"parse": []},
                    },
                )
                response.raise_for_status()
            return True
        except (httpx.HTTPError, ValueError):
            LOGGER.exception("Discord research analytics failed")
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
            "name": "📅 7-Day Path Context",
            "value": self._weekly_summary(weekly),
            "inline": False,
        })
        return {
            "title": title,
            "description": (
                f"**{matrix.total_signals}** traced signals • Historical outcomes and adverse excursions.\n"
                "Raw signal analytics only — no trading or risk-management strategy is assumed."
            ),
            "color": color,
            "fields": fields,
        }

    def _strategy_row_title(self, row: StrategyRowSummary) -> str:
        if row.strategy == "profit_20":
            return "🎯 +20% Target Race • Horizon Independent"
        if row.horizon_hours is not None:
            return f"⏱️ {self._horizon_label(row.horizon_hours)} Outcomes"
        return f"⏱️ {row.label}"

    def _strategy_row_value(self, row: StrategyRowSummary) -> str:
        if row.strategy == "profit_20":
            return "\n".join(self._target_race_line(cell) for cell in row.thresholds)

        if not row.thresholds:
            return "⏳ No matured signals yet."
        base = row.thresholds[0]
        if base.total == 0:
            return "⏳ No matured signals yet."
        profitable_rate = base.win_rate
        not_profitable_rate = (base.failures / base.total) if base.total else None
        breaches = " • ".join(
            f"-{cell.adverse_limit_pct}% **{cell.breach_failures}**" for cell in row.thresholds
        )
        return (
            f"**{base.total}** matured • Profitable **{base.wins}/{base.total} ({self._percent(profitable_rate)})** • "
            f"Not profitable **{base.failures}/{base.total} ({self._percent(not_profitable_rate)})**\n"
            f"Avg raw **{self._signed_percent(base.avg_profit)}** • Σ raw **{self._signed_percent(base.sum_profit)}**\n"
            f"Adverse crossed before horizon: {breaches}"
        )

    def _target_race_line(self, cell: StrategyThresholdSummary) -> str:
        threshold = f"-{cell.adverse_limit_pct}%"
        base = (
            f"**{threshold} adverse:** Target-first **{self._percent(cell.win_rate)}** • "
            f"target first {cell.wins}/{cell.resolved} resolved • breach first {cell.failures}"
        )
        if cell.pending:
            base += f" • pending {cell.pending}"
        if cell.avg_time_to_target_hours is not None:
            base += f" • avg t **{self._hours(cell.avg_time_to_target_hours)}**"
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

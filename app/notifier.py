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
from app.research_analytics import ResearchAnalyticsReport
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
        if risk_tier == "extreme_risk":
            LOGGER.info("Discord signal suppressed for EXTREME_RISK %s", signal.symbol)
            return
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
        """Send the compact subscriber-facing strategy board.

        The audience-facing board exposes only the three retained observed approaches:
        TP5 for frequent STANDARD + HIGH_RISK trading, open-ended TP20 for HIGH_RISK
        swing trading, and a fixed 7-day hold for STANDARD signals. Headline account
        run-rates replay the recommended sizing/capacity chronologically; exploratory
        strategy research remains internal.
        """
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
        tp5 = report.tp5_7d_comparison
        tp20 = report.tp20_7d_comparison
        swing = report.standard_7d_comparison

        def comparison_range_text() -> str:
            if report.comparison_start_at is None or report.comparison_end_at is None:
                return "No complete common 7-day cohort yet."
            start_at = report.comparison_start_at
            end_at = report.comparison_end_at
            if timezone_name:
                tz = ZoneInfo(timezone_name)
                start_at = start_at.astimezone(tz)
                end_at = end_at.astimezone(tz)
            return (
                f"Signal cohort **{start_at.strftime('%d %b %Y')} → {end_at.strftime('%d %b %Y')}** • "
                "every headline return is valued exactly **168h after entry**."
            )

        def exposure_line(rec, *, tested: bool) -> str:
            stress_100 = rec.per_trade_pct
            stress_200 = rec.per_trade_pct * 2.0
            basis = "portfolio-tested" if tested else "risk-based suggestion"
            return (
                f"Suggested account: **{self._percent(rec.per_trade_pct)} / trade** • "
                f"max **{rec.max_slots}** positions • **{self._percent(rec.max_account_exposure_pct)}** total cap • 1× "
                f"({basis})\n"
                f"1-position stress: +100% adverse ≈ **-{self._percent(stress_100)} equity** • "
                f"+200% ≈ **-{self._percent(stress_200)}**"
            )

        def risk_line(strategy) -> str:
            worst_adverse = (
                f"-{self._percent(strategy.worst_adverse)}"
                if strategy.worst_adverse is not None else "n/a"
            )
            return (
                f"Breaches before exit/7D mark: -50% **{strategy.breach_50}** • "
                f"-100% **{strategy.breach_100}** • -200% **{strategy.breach_200}** • "
                f"-300% **{strategy.breach_300}** • worst adverse **{worst_adverse}**"
            )

        def signed_money(value: object) -> str:
            if value is None:
                return "n/a"
            number = float(value)
            sign = "+" if number >= 0 else "-"
            return f"{sign}${abs(number):,.0f}"

        def account_run_rate_line(label: str, summary) -> str:
            if summary is None:
                return f"**{label}:** unavailable"
            return (
                f"**{label}:** observed account **{self._signed_percent(summary.observed_account_return)}** over **{summary.span_days:.2f}d** "
                f"→ **{self._signed_percent(summary.thirty_day_equivalent_return)} 30D eq.** "
                f"({signed_money(summary.thirty_day_pnl_per_10k)} / $10k) • "
                f"entered **{summary.entered}** • open **{summary.open_positions}** • "
                f"capacity misses **{summary.missed_capacity}** • avg/peak exposure "
                f"**{self._percent(summary.avg_exposure_pct)} / {self._percent(summary.peak_exposure_pct)}**"
            )

        account_comparison_value = "\n".join((
            account_run_rate_line("TP5", report.tp5_account_run_rate),
            account_run_rate_line("TP20", report.tp20_account_run_rate),
            account_run_rate_line("7D", report.standard_7d_account_run_rate),
        ))

        if tp5 is not None:
            tp5_value = (
                f"**Rule:** STANDARD + HIGH RISK • full close at **+5%**; no forced timeout\n"
                f"7D normalized **{tp5.sample}** • target hits **{tp5.target_hits}** • open at 7D **{tp5.unresolved_at_7d}** • "
                f"profitable mark **{tp5.wins}/{tp5.sample} ({self._percent(tp5.positive_rate)})**\n"
                f"Σ signal **{self._signed_percent(tp5.sum_return)}** • avg/signal **{self._signed_percent(tp5.avg_return)}** • "
                f"median **{self._signed_percent(tp5.median_return)}** • best/worst **{self._signed_percent(tp5.best_return)} / {self._signed_percent(tp5.worst_return)}**\n"
                f"Avg / median capital time to exit-or-mark **{self._hours(tp5.avg_effective_holding_hours)} / {self._hours(tp5.median_effective_holding_hours)}**\n"
                f"{risk_line(tp5)}\n"
                f"{exposure_line(report.tp5_exposure, tested=True)}"
            )
        else:
            tp5_value = "No complete 7-day comparison cohort yet."

        if tp20 is not None:
            tp20_value = (
                f"**Rule:** HIGH_RISK only • full close at **+20%**; otherwise **stay open**\n"
                f"7D normalized **{tp20.sample}** • TP20 hits by 7D **{tp20.target_hits}** • open at 7D **{tp20.unresolved_at_7d}** • "
                f"profitable mark **{tp20.wins}/{tp20.sample} ({self._percent(tp20.positive_rate)})**\n"
                f"Σ signal **{self._signed_percent(tp20.sum_return)}** • avg/signal **{self._signed_percent(tp20.avg_return)}** • "
                f"median **{self._signed_percent(tp20.median_return)}** • best/worst **{self._signed_percent(tp20.best_return)} / {self._signed_percent(tp20.worst_return)}**\n"
                f"Avg / median capital time to exit-or-mark **{self._hours(tp20.avg_effective_holding_hours)} / {self._hours(tp20.median_effective_holding_hours)}**\n"
                f"{risk_line(tp20)}\n"
                f"{exposure_line(report.tp20_exposure, tested=False)}"
            )
        else:
            tp20_value = "No complete HIGH_RISK 7-day comparison cohort yet."

        if swing is not None:
            swing_value = (
                f"**Rule:** STANDARD only • short at confirmation and close exactly at **7 days**\n"
                f"7D normalized **{swing.sample}** • wins **{swing.wins}** • losses **{swing.losses}** • "
                f"win rate **{self._percent(swing.positive_rate)}**\n"
                f"Σ signal **{self._signed_percent(swing.sum_return)}** • avg/signal **{self._signed_percent(swing.avg_return)}** • "
                f"median **{self._signed_percent(swing.median_return)}** • best/worst **{self._signed_percent(swing.best_return)} / {self._signed_percent(swing.worst_return)}**\n"
                f"Capital time **7.00d fixed**\n"
                f"{risk_line(swing)}\n"
                f"{exposure_line(report.standard_7d_exposure, tested=False)}"
            )
        else:
            swing_value = "No complete STANDARD 7-day comparison cohort yet."

        board = {
            "title": "📊 Exhaustion Scanner • Strategy Comparison",
            "description": (
                f"**{self._pretty_label(label)}**\n"
                f"Updated **{as_of_text}**\n\n"
                f"{comparison_range_text()}\n"
                "Equal-notional signal returns • EXTREME_RISK signals are not published."
            ),
            "color": 0x5865F2,
            "fields": [
                {
                    "name": "💰 Account-Level Return • Recommended Sizing",
                    "value": account_comparison_value,
                    "inline": False,
                },
                {
                    "name": "⚡ TP5 Frequent",
                    "value": tp5_value,
                    "inline": False,
                },
                {
                    "name": "🔥 TP20 High Risk • No Timeout",
                    "value": tp20_value,
                    "inline": False,
                },
                {
                    "name": "🗓️ 7D Swing • STANDARD",
                    "value": swing_value,
                    "inline": False,
                },
                {
                    "name": "How to choose",
                    "value": (
                        "**TP5:** fastest recycling and the only exposure setup already portfolio-tested.\n"
                        "**TP20:** larger target, but unresolved HIGH_RISK positions can remain open beyond day 7 and carry the widest tail risk.\n"
                        "**7D Swing:** fixed holding period and STANDARD-only exposure; compare its larger raw moves against seven days of capital lock-up.\n"
                        "Use **account return / 30D equivalent** for the practical strategy comparison; Σ signal is supporting opportunity data only."
                    ),
                    "inline": False,
                },
                {
                    "name": "Today",
                    "value": (
                        f"Confirmed public signals **{report.confirmed_today}** • "
                        f"currently tracking **{report.open_count}**"
                    ),
                    "inline": False,
                },
            ],
            "footer": {
                "text": (
                    "Account replay uses the same observed calendar, chronological signals, recommended slot caps, and 0.08% fee per fill; open positions are MTM at report time. "
                    "30D eq. is a linear run-rate from the observed span, not an observed 30-day result or forecast; funding/slippage are not modeled. "
                    "The 168h signal table remains normalized for path comparison; TP20 itself has no day-7 exit. Σ signal is not account return. "
                    "TP20/7D sizing is risk-based, not a validated optimum; caps apply account-wide and should not be blindly stacked."
                )
            },
        }

        try:
            self._validate_discord_embed(board)
            response = await self._client.post(
                self._performance_webhook_url,
                json={
                    "username": "Exhaustion Scanner • Stats",
                    "embeds": [board],
                    "allowed_mentions": {"parse": []},
                },
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

        tp5_outcomes = [item.tp5_strategy for item in ledger.items]
        tp20_outcomes = [item.tp20_strategy for item in ledger.items if item.tp20_strategy.eligible]
        swing_outcomes = [item.standard_7d_strategy for item in ledger.items if item.standard_7d_strategy.eligible]

        def breach_line(outcomes) -> str:
            return (
                f"-50% **{sum(o.breach_50_before_effective for o in outcomes)}** • "
                f"-100% **{sum(o.breach_100_before_effective for o in outcomes)}** • "
                f"-200% **{sum(o.breach_200_before_effective for o in outcomes)}** • "
                f"-300% **{sum(o.breach_300_before_effective for o in outcomes)}**"
            )

        tp5_hits = sum(o.state == "target_hit" for o in tp5_outcomes)
        tp5_open = sum(o.state == "open" for o in tp5_outcomes)
        tp20_hits = sum(o.state == "target_hit" for o in tp20_outcomes)
        tp20_open = sum(o.state == "open" for o in tp20_outcomes)
        swing_closed = [o for o in swing_outcomes if o.state in {"closed_win", "closed_loss"}]
        swing_wins = sum(o.state == "closed_win" for o in swing_closed)
        swing_tracking = sum(o.state == "tracking" for o in swing_outcomes)

        summary = {
            "title": "📒 Exhaustion Scanner • Strategy Ledger",
            "description": (
                f"Updated **{display_time.strftime('%d %b %Y • %H:%M %Z')}**\n"
                "Per-signal audit for TP5 Frequent, TP20 High Risk No Timeout, and STANDARD 7D Swing • full strategy flags attached as CSV"
            ),
            "color": 0x5865F2,
            "fields": [
                {
                    "name": "📦 Signals",
                    "value": (
                        f"**{ledger.total}** public signals • "
                        f"🟢 STANDARD **{risk_counts['standard']}** • "
                        f"🟡 HIGH **{risk_counts['high_risk']}**"
                    ),
                    "inline": False,
                },
                {
                    "name": "⚡ TP5 Frequent • STANDARD + HIGH",
                    "value": (
                        f"Target hit **{tp5_hits}/{len(tp5_outcomes)}** • still open **{tp5_open}**\n"
                        f"Breach before target/current mark: {breach_line(tp5_outcomes)}"
                    ),
                    "inline": False,
                },
                {
                    "name": "🔥 TP20 High Risk • No Timeout",
                    "value": (
                        f"Eligible **{len(tp20_outcomes)}** • target hit **{tp20_hits}** • still open **{tp20_open}**\n"
                        f"Breach before target/current mark: {breach_line(tp20_outcomes)}"
                    ),
                    "inline": False,
                },
                {
                    "name": "🗓️ 7D Swing • STANDARD only",
                    "value": (
                        f"Eligible **{len(swing_outcomes)}** • closed **{len(swing_closed)}** • "
                        f"wins **{swing_wins}** • tracking **{swing_tracking}**\n"
                        f"Breach before 7D exit/current mark: {breach_line(swing_outcomes)}"
                    ),
                    "inline": False,
                },
                {
                    "name": "How to read the table",
                    "value": (
                        "Each strategy cell shows its own outcome and deepest adverse threshold carried before that strategy's target/exit. "
                        "`pre -100%` means -100% occurred before exit; `so far -50%` means an open/tracking trade has crossed -50% so far. "
                        "TP20 is HIGH-only; 7D Swing is STANDARD-only."
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
                    "description": f"Page **{table.page}/{table.total_pages}** • strategy outcome + pre-target/pre-exit breach • exact flags in CSV",
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
        """Send the compact research validation board.

        The analytics engine still retains historical exploratory studies, but the
        visible research report is intentionally limited to evidence health and
        prospective TP5 validation. The public Strategy Comparison owns the three
        subscriber-facing strategy choices so Discord never shows conflicting rules.
        """
        if not self._performance_webhook_url:
            return False

        tz = ZoneInfo(timezone_name)
        display_time = (as_of or report.generated_at).astimezone(tz)
        b = report.baseline
        completeness_7d = (b.complete_paths_7d / b.matured_7d) if b.matured_7d else None
        tp5 = report.tp5_risk
        calendar = report.calendar_throughput
        live_tp5 = report.prospective_tp5_live

        tp5_entries_per_day = (
            calendar.tp5.entered / calendar.history_span_days
            if calendar.history_span_days > 0 else None
        )
        tp5_releases_per_day = (
            calendar.tp5.closed / calendar.history_span_days
            if calendar.history_span_days > 0 else None
        )

        strategy_board = {
            "title": "🔬 Exhaustion Scanner • Strategy Validation",
            "description": (
                f"Updated **{display_time.strftime('%d %b %Y • %H:%M %Z')}**\n"
                "Exploratory variants remain internal. Subscriber strategy selection lives in the normalized **Strategy Comparison** report."
            ),
            "color": 0x5865F2,
            "fields": [
                {
                    "name": "📦 Evidence base",
                    "value": (
                        f"Signals **{b.total_signals}** • 7d matured **{b.matured_7d}** • "
                        f"complete paths **{b.complete_paths_7d}/{b.matured_7d} ({self._percent(completeness_7d)})**"
                    ),
                    "inline": False,
                },
                {
                    "name": "⚡ TP5 Frequent • Frozen portfolio monitor",
                    "value": (
                        f"7d-evaluable **{tp5.sample}** • TP5 hits **{tp5.hits}/{tp5.sample} ({self._percent(tp5.hit_rate)})** • "
                        f"median / p75 **{self._hours(tp5.median_time_hours)} / {self._hours(tp5.p75_time_hours)}**\n"
                        f"Median pre-hit adverse **-{self._percent(tp5.median_adverse_before_target)}** • "
                        f"worst **-{self._percent(tp5.worst_adverse_before_target)}**\n"
                        f"Observed {calendar.history_span_days:.2f}d portfolio return **{self._signed_percent(calendar.tp5.marked_return)}** • "
                        f"MTM DD **{'-' + self._percent(calendar.tp5.max_mtm_drawdown) if calendar.tp5.max_mtm_drawdown is not None else 'n/a'}** • "
                        f"entries/day **{tp5_entries_per_day:.2f}** • releases/day **{tp5_releases_per_day:.2f}**"
                    ),
                    "inline": False,
                },
                {
                    "name": "Subscriber report",
                    "value": (
                        "Use **Strategy Comparison** for **TP5 Frequent**, **TP20 High Risk • No Timeout**, and **7D Swing • STANDARD**. "
                        "That board uses one common 168h valuation convention, breach counts, and suggested account exposure."
                    ),
                    "inline": False,
                },
            ],
            "footer": {
                "text": "EXTREME_RISK is excluded. TP1/TP2/hybrids/EntryGate/feature sweeps remain internal and are intentionally not published."
            },
        }

        if calendar.latest_30d_tp5 is not None:
            thirty_day = (
                f"True 30d TP5 replay: return **{self._signed_percent(calendar.latest_30d_tp5.marked_return)}** • "
                f"DD **{'-' + self._percent(calendar.latest_30d_tp5.max_mtm_drawdown) if calendar.latest_30d_tp5.max_mtm_drawdown is not None else 'n/a'}**"
            )
        else:
            thirty_day = (
                f"First true 30d empty-book replay not available yet • observed **{calendar.history_span_days:.2f}d** • "
                f"need **{calendar.days_until_30d:.2f}d** more."
            )

        prospective = {
            "title": "📡 TP5 • Prospective Monitor",
            "description": (
                f"Frozen **{report.oos_freeze_at.astimezone(tz).strftime('%d %b %Y • %H:%M %Z')}**. "
                "This is the real forward-validation stream; no retuning from these observations."
            ),
            "color": 0x1ABC9C,
            "fields": [
                {
                    "name": "Post-freeze TP5 tracker",
                    "value": (
                        f"Signals **{live_tp5.signals}** • hit **{live_tp5.hits}** • waiting **{live_tp5.waiting}** • "
                        f"failed after complete 7d **{live_tp5.failed}**\n"
                        f"Resolved hit rate **{self._percent(live_tp5.resolved_hit_rate)}** • "
                        f"median / p75 **{self._hours(live_tp5.median_hit_hours)} / {self._hours(live_tp5.p75_hit_hours)}**\n"
                        f"Worst pre-hit adverse **-{self._percent(live_tp5.worst_pre_hit_adverse)}** • "
                        f"oldest waiting **{self._hours(live_tp5.oldest_waiting_hours)}**"
                    ),
                    "inline": False,
                },
                {
                    "name": "30-day validation",
                    "value": thirty_day,
                    "inline": False,
                },
            ],
            "footer": {"text": "Fast TP5 monitoring is descriptive until the forward cohort fully matures."},
        }

        embeds = (strategy_board, prospective)
        try:
            for embed in embeds:
                self._validate_discord_embed(embed)

            payload = {
                "username": "Exhaustion Scanner • Research",
                "embeds": [strategy_board],
                "allowed_mentions": {"parse": []},
            }
            if dataset_csv is not None:
                response = await self._client.post(
                    self._performance_webhook_url,
                    data={"payload_json": json.dumps(payload)},
                    files={
                        "files[0]": (
                            f"research-signal-dataset-{display_time.strftime('%Y-%m-%d')}.csv",
                            dataset_csv,
                            "text/csv",
                        )
                    },
                )
            else:
                response = await self._client.post(self._performance_webhook_url, json=payload)
            response.raise_for_status()

            response = await self._client.post(
                self._performance_webhook_url,
                json={
                    "username": "Exhaustion Scanner • Research",
                    "embeds": [prospective],
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

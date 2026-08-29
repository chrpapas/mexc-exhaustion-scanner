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
        """Send the subscriber-facing three-strategy comparison.

        Every strategy uses the same public confirmed-short stream, 6 generic 5%
        slots and 30% aggregate exposure. Only exit policy changes, which keeps the
        comparison understandable and replicable.
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

        strategies = [
            ("⚡ A • TP5 indefinite", report.trader_strategy_tp5, report.tp5_account_run_rate),
            ("🛡️ B • TP5 + SL75 • default", report.trader_strategy_tp5_sl75, report.tp5_sl75_account_run_rate),
            ("🗓️ C • TP5 + 7D cutoff", report.trader_strategy_tp5_7d, report.tp5_7d_account_run_rate),
        ]

        def account_line(account) -> str:
            if account is None:
                return "Account replay unavailable"
            dd = (
                f"-{self._percent(account.max_mtm_drawdown)}"
                if account.max_mtm_drawdown is not None else "n/a"
            )
            ratio = (
                f"{account.return_over_max_drawdown:.2f}×"
                if account.return_over_max_drawdown is not None else "n/a"
            )
            return (
                f"Account **{self._signed_percent(account.observed_account_return)}** over **{account.span_days:.2f}d** • "
                f"DD **{dd}** • R/DD **{ratio}**\n"
                f"entered **{account.entered}** • closed **{account.closed}** • open **{account.open_positions}** • "
                f"capacity/symbol misses **{account.missed_capacity}/{account.missed_same_symbol}** • "
                f"avg/peak exposure **{self._percent(account.avg_exposure_pct)} / {self._percent(account.peak_exposure_pct)}**"
            )

        def signal_line(summary) -> str:
            if summary is None:
                return "Signal summary unavailable"
            mix = [f"TP5 **{summary.target_exits}**"]
            if summary.stop_exits:
                mix.append(f"SL75 **{summary.stop_exits}**")
            if summary.timeout_exits:
                mix.append(f"7D close **{summary.timeout_exits}**")
            mix.append(f"waiting **{summary.waiting}**")
            return (
                f"**Rule:** {summary.rule}\n"
                f"Signals **{summary.sample}** • {' • '.join(mix)} • resolved profitable **{self._percent(summary.resolved_positive_rate)}**\n"
                f"median / p75 hold **{self._hours(summary.median_holding_hours)} / {self._hours(summary.p75_holding_hours)}** • "
                f"tails before exit/current mark: -50% **{summary.breach_50}** • -75% **{summary.breach_75}** • -100% **{summary.breach_100}**"
            )

        fields = [
            {
                "name": "Replication settings",
                "value": (
                    "Enter every published **STANDARD + HIGH_RISK confirmed-short** signal • **1× cross** • "
                    "**5% of current equity per trade** • max **6** simultaneous positions • **30%** aggregate cap • "
                    "one open position per symbol. Research accounting assumes **0.08% fee per fill**."
                ),
                "inline": False,
            }
        ]
        for title, summary, account in strategies:
            fields.append({
                "name": title,
                "value": f"{signal_line(summary)}\n{account_line(account)}",
                "inline": False,
            })
        fields.extend([
            {
                "name": "How to read this",
                "value": (
                    "Compare **account return, MTM drawdown, return/DD, open positions and tail counts** before looking at hit rate. "
                    "A high TP5 hit rate can still be unattractive if unresolved shorts create large tail losses. "
                    "The 7D variant forcibly closes unresolved positions at exactly 168h; A and B have no time expiry."
                ),
                "inline": False,
            },
            {
                "name": "Today",
                "value": f"Confirmed public signals **{report.confirmed_today}** • currently tracking **{report.open_count}**",
                "inline": False,
            },
        ])

        board = {
            "title": "📊 Exhaustion Scanner • Strategy Comparison",
            "description": (
                f"**{self._pretty_label(label)}**\nUpdated **{as_of_text}**\n"
                "Three exit policies on the same entries and sizing. **TP5+SL75 is the current trader default.**"
            ),
            "color": 0x5865F2,
            "fields": fields,
            "footer": {
                "text": (
                    "Account replay is chronological and includes capacity, 0.08% entry/exit fees and current MTM for open positions. "
                    "Max MTM DD uses stored 15m research paths where available. Funding and execution slippage are not modeled; same-candle TP5/SL75 is stop-first."
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
                "Per-signal observational audit for TP5, TP20 No Timeout, and 7D Hold across every published signal • full strategy flags attached as CSV"
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
                    "name": "🔥 TP20 No Timeout • Observational",
                    "value": (
                        f"Observed **{len(tp20_outcomes)}** • target hit **{tp20_hits}** • still open **{tp20_open}**\n"
                        f"Breach before target/current mark: {breach_line(tp20_outcomes)}"
                    ),
                    "inline": False,
                },
                {
                    "name": "🗓️ 7D Hold • Observational",
                    "value": (
                        f"Observed **{len(swing_outcomes)}** • closed **{len(swing_closed)}** • "
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
                        "The ledger is observational: TP20 and 7D are shown for both STANDARD and HIGH_RISK. Subscriber recommendation filters are applied only in Strategy Comparison."
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
        sweeps_csv: bytes | None = None,
        entry_csv: bytes | None = None,
        regime_csv: bytes | None = None,
        as_of: datetime | None = None,
        timezone_name: str = "Europe/Zurich",
    ) -> bool:
        """Send a focused trader-facing board plus machine-readable research files.

        The visible comparison holds entry universe, sizing and capacity constant and
        varies only the exit policy: TP5 indefinite, TP5+SL75, and TP5+7D cutoff.
        Exploratory feature/regime studies stay in the exported research data rather
        than competing for space on the subscriber decision surface.
        """
        if not self._performance_webhook_url:
            return False

        tz = ZoneInfo(timezone_name)
        display_time = (as_of or report.generated_at).astimezone(tz)
        b = report.baseline
        calendar = report.calendar_throughput
        validations = {item.strategy: item for item in report.strategy_validations}
        portfolios = {
            "tp5_challenger": report.portfolio_tp5,
            "tp5_sl75_challenger": report.portfolio_tp5_sl75,
            "tp5_7d_cutoff": report.portfolio_tp5_7d_cutoff,
        }
        prospective_validations = {item.strategy: item for item in report.prospective_strategy_validations}
        prospective_portfolios = dict(zip(
            ("tp5_challenger", "tp5_sl75_challenger", "tp5_7d_cutoff"),
            report.prospective_strategy_portfolios,
        ))
        completeness_7d = (b.complete_paths_7d / b.matured_7d) if b.matured_7d else None

        def tail_text(summary) -> str:
            tails = {item.threshold_pct: item for item in summary.tail_ladder}
            return " • ".join(
                f"-{threshold}% **{tails[threshold].breached_before_exit_or_mark}**"
                for threshold in (20, 50, 75, 100)
                if threshold in tails
            )

        def exit_mix(summary) -> str:
            parts = [f"TP5 **{summary.target_exits}**"]
            if summary.stop_exits:
                parts.append(f"SL75 **{summary.stop_exits}**")
            if summary.timeout_exits:
                parts.append(f"7D close **{summary.timeout_exits}**")
            parts.append(f"waiting **{summary.waiting}**")
            return " • ".join(parts)

        def portfolio_line(portfolio) -> str:
            dd = (
                f"-{self._percent(portfolio.max_mtm_drawdown)}"
                if portfolio.max_mtm_drawdown is not None else "n/a"
            )
            ratio = (
                f"{portfolio.return_over_max_drawdown:.2f}×"
                if portfolio.return_over_max_drawdown is not None else "n/a"
            )
            return (
                f"Account MTM **{self._signed_percent(portfolio.marked_return)}** • "
                f"realized **{self._signed_percent(portfolio.realized_return)}** • DD **{dd}** • R/DD **{ratio}**\n"
                f"entered **{portfolio.entered}** • closed **{portfolio.closed}** • open **{portfolio.open_positions}** • "
                f"capacity/symbol misses **{portfolio.missed_capacity}/{portfolio.missed_same_symbol}**"
            )

        def strategy_field(strategy: str, title: str) -> dict:
            summary = validations[strategy]
            portfolio = portfolios[strategy]
            return {
                "name": title,
                "value": (
                    f"**Rule:** {summary.rule}\n"
                    f"Signals **{summary.sample}** • {exit_mix(summary)} • resolved profitable **{self._percent(summary.resolved_positive_rate)}**\n"
                    f"median / p75 hold **{self._hours(summary.median_holding_hours)} / {self._hours(summary.p75_holding_hours)}** • "
                    f"tails before exit/current mark: {tail_text(summary)}\n"
                    f"{portfolio_line(portfolio)}"
                ),
                "inline": False,
            }

        strategy_board = {
            "title": "📊 Exhaustion Scanner • 3-Strategy Validation",
            "description": (
                f"Updated **{display_time.strftime('%d %b %Y • %H:%M %Z')}**\n"
                "Same entries for every comparison: **STANDARD + HIGH_RISK confirmed shorts • 6 slots × 5% current equity • 30% cap • 1× • one position/symbol**. "
                "Research assumes **0.08% fee per fill**. Only the exit rule changes."
            ),
            "color": 0x5865F2,
            "fields": [
                {
                    "name": "📦 Evidence",
                    "value": (
                        f"Observed signals **{b.total_signals}** over **{calendar.history_span_days:.2f}d** • "
                        f"7d matured **{b.matured_7d}** • complete 7d paths **{b.complete_paths_7d}/{b.matured_7d} ({self._percent(completeness_7d)})** • "
                        f"complete 14d paths **{b.complete_paths_14d}**.\n"
                        "7d maturity is used for fixed-horizon evidence only; it does **not** expire TP5-indefinite or TP5+SL75 positions."
                    ),
                    "inline": False,
                },
                strategy_field("tp5_challenger", "⚡ A • TP5 indefinite"),
                strategy_field("tp5_sl75_challenger", "🛡️ B • TP5 + SL75 • current default"),
                strategy_field("tp5_7d_cutoff", "🗓️ C • TP5 + 7D cutoff"),
                {
                    "name": "How to compare",
                    "value": (
                        "Prioritize **account MTM return, max MTM drawdown, return/DD, unresolved/open positions and tail counts**. "
                        "Signal hit rate alone can hide large open losses. The 7D strategy exits unresolved trades at the 168h mark; the other two have no time expiry."
                    ),
                    "inline": False,
                },
            ],
            "footer": {
                "text": "Research replay uses chronological arrivals and capacity. Funding/slippage are not modeled; same-candle TP5/SL75 races are conservatively stop-first."
            },
        }

        prospective_lines: list[str] = []
        for strategy, label in (
            ("tp5_challenger", "TP5 indefinite"),
            ("tp5_sl75_challenger", "TP5+SL75"),
            ("tp5_7d_cutoff", "TP5+7D"),
        ):
            summary = prospective_validations[strategy]
            portfolio = prospective_portfolios[strategy]
            extra = (
                f" • open >7d **{report.prospective_tp5_live.waiting_over_7d}**"
                if strategy == "tp5_challenger" else ""
            )
            prospective_lines.append(
                f"**{label}:** {exit_mix(summary)}{extra} • MTM **{self._signed_percent(portfolio.marked_return)}** • "
                f"DD **{'-' + self._percent(portfolio.max_mtm_drawdown) if portfolio.max_mtm_drawdown is not None else 'n/a'}** • entered **{portfolio.entered}**"
            )

        if all(item is not None for item in (
            calendar.latest_30d_tp5, calendar.latest_30d_tp5_sl75, calendar.latest_30d_tp5_7d_cutoff
        )):
            thirty_day = "\n".join((
                f"TP5 indefinite **{self._signed_percent(calendar.latest_30d_tp5.marked_return)}** / DD **-{self._percent(calendar.latest_30d_tp5.max_mtm_drawdown)}**",
                f"TP5+SL75 **{self._signed_percent(calendar.latest_30d_tp5_sl75.marked_return)}** / DD **-{self._percent(calendar.latest_30d_tp5_sl75.max_mtm_drawdown)}**",
                f"TP5+7D **{self._signed_percent(calendar.latest_30d_tp5_7d_cutoff.marked_return)}** / DD **-{self._percent(calendar.latest_30d_tp5_7d_cutoff.max_mtm_drawdown)}**",
            ))
        else:
            thirty_day = (
                f"True 30d empty-book comparison not available yet • observed **{calendar.history_span_days:.2f}d** • "
                f"need **{calendar.days_until_30d:.2f}d** more."
            )

        prospective = {
            "title": "📡 Forward Validation • Frozen 21 Aug",
            "description": (
                f"Post-freeze evidence only. Frozen **{report.oos_freeze_at.astimezone(tz).strftime('%d %b %Y • %H:%M %Z')}**; "
                "do not tune thresholds from this stream and then call the result out-of-sample."
            ),
            "color": 0x1ABC9C,
            "fields": [
                {"name": "Post-freeze strategies", "value": "\n".join(prospective_lines), "inline": False},
                {"name": "True latest-30d empty-book replay", "value": thirty_day, "inline": False},
                {
                    "name": "Files for deeper analysis",
                    "value": (
                        "**strategy-validation.csv** = compact strategy-level decision table.\n"
                        "**research-signal-dataset.csv** = per-signal features, path statistics, target/adverse timestamps, and explicit outcome columns for all three strategies."
                    ),
                    "inline": False,
                },
            ],
            "footer": {
                "text": "Exploratory feature, regime, EntryGate and TP1/TP2 studies remain in internal research exports; they are intentionally kept off this trader-facing comparison."
            },
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
            files: dict[str, tuple[str, bytes, str]] = {}
            if dataset_csv is not None:
                files[f"files[{len(files)}]"] = (
                    f"research-signal-dataset-{display_time.strftime('%Y-%m-%d')}.csv",
                    dataset_csv,
                    "text/csv",
                )
            if strategy_csv is not None:
                files[f"files[{len(files)}]"] = (
                    f"strategy-validation-{display_time.strftime('%Y-%m-%d')}.csv",
                    strategy_csv,
                    "text/csv",
                )
            if feature_csv is not None:
                files[f"files[{len(files)}]"] = (
                    f"feature-lift-{display_time.strftime('%Y-%m-%d')}.csv",
                    feature_csv,
                    "text/csv",
                )
            if sweeps_csv is not None:
                files[f"files[{len(files)}]"] = (
                    f"strategy-sweeps-{display_time.strftime('%Y-%m-%d')}.csv",
                    sweeps_csv,
                    "text/csv",
                )
            if entry_csv is not None:
                files[f"files[{len(files)}]"] = (
                    f"entry-research-{display_time.strftime('%Y-%m-%d')}.csv",
                    entry_csv,
                    "text/csv",
                )
            if regime_csv is not None:
                files[f"files[{len(files)}]"] = (
                    f"token-regime-{display_time.strftime('%Y-%m-%d')}.csv",
                    regime_csv,
                    "text/csv",
                )
            if files:
                response = await self._client.post(
                    self._performance_webhook_url,
                    data={"payload_json": json.dumps(payload)},
                    files=files,
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

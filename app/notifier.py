from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.models import RunSignal
from app.daily_core_strategy import (
    DAILY_CORE_SKIP_STRATEGY,
    daily_confirmed_core_v1_missing_features,
    daily_confirmed_core_v1_state,
)
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
        subscriber_signal_strategy: str = "all_confirmed",
    ) -> None:
        self._webhook_url = webhook_url
        # Backward-compatible fallback: if the dedicated stats webhook is not
        # configured, reports continue to go to the existing Discord webhook.
        self._performance_webhook_url = performance_webhook_url or webhook_url
        self._signal_levels = frozenset(signal_levels or {"confirmed_short"})
        self._subscriber_signal_strategy = subscriber_signal_strategy
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
        if self._subscriber_signal_strategy == DAILY_CORE_SKIP_STRATEGY:
            daily_core_state = daily_confirmed_core_v1_state(features)
            if daily_core_state is None:
                missing = daily_confirmed_core_v1_missing_features(features)
                LOGGER.warning(
                    "Subscriber signal suppressed fail-closed for %s: missing Daily-Core inputs=%s",
                    signal.symbol,
                    ",".join(missing) or "unknown",
                )
                return
            if daily_core_state:
                LOGGER.info(
                    "Subscriber signal hard-filtered by Daily-Confirmed Core V1: %s",
                    signal.symbol,
                )
                return
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
        """Send the subscriber performance board and replication playbook.

        The board deliberately separates all-signal economics (no capacity limit)
        from the chronological account replay (6 x 5% slots / 30% cap). This keeps
        raw signal edge, portfolio constraints and the monthly run-rate from being
        mistaken for the same statistic.
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
            (
                "🕰️ Previous active • TP5 + SL75 • PCR de-risk",
                report.trader_strategy_tp5_sl75,
                report.tp5_sl75_pcr_account_run_rate,
                "PCR 2.5/5% account",
            ),
            (
                "🛡️ Current • TP5 + SL75 • Daily-Core hard filter",
                report.trader_strategy_tp5_sl75,
                report.tp5_sl75_daily_core_skip_account_run_rate,
                "Daily-Core hard-filter account",
            ),
        ]

        def exit_mix(summary) -> str:
            if summary is None:
                return "n/a"
            if summary.strategy == "hold_7d":
                return f"7D closed **{summary.timeout_exits}** • still <7D **{summary.waiting}**"
            parts = [f"TP5 **{summary.target_exits}**"]
            if summary.stop_exits:
                parts.append(f"SL75 **{summary.stop_exits}**")
            parts.append(f"open **{summary.waiting}**")
            return " • ".join(parts)

        def signal_economics(summary) -> str:
            if summary is None:
                return "All-signal economics unavailable"
            return (
                f"All signals, no slot cap: **net Σ {self._signed_percent(summary.sum_marked_return)}** marked trade return • "
                f"avg **{self._signed_percent(summary.avg_marked_return)}**/trade • positive **{self._percent(summary.marked_positive_rate)}** "
                f"({summary.marked_sample}/{summary.sample} marked)\n"
                f"{exit_mix(summary)} • median/p75 completed hold **{self._hours(summary.median_holding_hours)} / {self._hours(summary.p75_holding_hours)}**"
            )

        def account_economics(account, account_label: str) -> str:
            if account is None:
                return f"{account_label} replay unavailable"
            dd = (
                f"-{self._percent(account.max_mtm_drawdown)}"
                if account.max_mtm_drawdown is not None else "n/a"
            )
            ratio = (
                f"{account.return_over_max_drawdown:.2f}×"
                if account.return_over_max_drawdown is not None else "n/a"
            )
            capture = (account.entered / account.eligible_signals) if account.eligible_signals else None
            return (
                f"{account_label}: observed **{self._signed_percent(account.observed_account_return)}** in **{account.span_days:.1f}d** → "
                f"**Est. monthly {self._signed_percent(account.thirty_day_equivalent_return)}*** • max DD **{dd}** • R/DD **{ratio}**\n"
                f"captured **{account.entered}/{account.eligible_signals} ({self._percent(capture)})** signals • "
                f"closed/open **{account.closed}/{account.open_positions}** • capacity/symbol misses **{account.missed_capacity}/{account.missed_same_symbol}** • "
                f"avg/peak exposure **{self._percent(account.avg_exposure_pct)} / {self._percent(account.peak_exposure_pct)}**"
            )

        fields = [
            {
                "name": "▶️ Suggested execution • current default",
                "value": (
                    "**TP5 + SL75 + Daily-Confirmed Core hard filter:** publish/trade only **STANDARD + HIGH_RISK confirmed-short** signals that pass the frozen 4h+1D admission rule • **1× cross** • "
                    "size each admitted entry at **5% of current equity** • **hard-skip** signals where Continuation Core V1 AND Daily Bull V1 are both true • "
                    "max **6** open positions • max **30% aggregate exposure** • one position per symbol • take profit at **+5% short return** • "
                    "catastrophic stop at **-75% short return** • no time expiry. Missing Daily-Core inputs fail closed.\n"
                    "Research assumes **0.08% fee per fill**. A 5% slot stopped at -75% is roughly **-3.75% account** before fees/slippage."
                ),
                "inline": False,
            },
        ]
        for title, summary, account, account_label in strategies:
            rule = summary.rule if summary is not None else "+5% TP or -75% SL, no timeout"
            if "PCR" in title:
                rule = f"{rule}; 2.5% size when 24h ≥30% and EMA20 extension ≥3 ATR, otherwise 5%"
            elif "Daily-Core" in title:
                rule = f"{rule}; hard-skip Daily-Confirmed Core V1 flagged/non-computable signals, otherwise fixed 5%"
            else:
                rule = f"{rule}; fixed 5% per entry"
            fields.append({
                "name": title,
                "value": f"**Rule:** {rule}\n{signal_economics(summary)}\n{account_economics(account, account_label)}",
                "inline": False,
            })

        fields.extend([
            {
                "name": "📖 What the numbers mean",
                "value": (
                    "**Net Σ trade return** asks what the shared TP5+SL75 signal stream produced if every qualifying signal were taken; it ignores capital/slot conflicts. "
                    "The chronological **account replay** is the main performance number and applies each competitor's actual sizing rule. "
                    "**Est. monthly*** linearly scales the observed account return to 30 days; it is a run-rate, **not a forecast**. "
                    "Use **MTM return + max DD + capture rate** together; win rate alone can hide open short tails."
                ),
                "inline": False,
            },
            {
                "name": "🧭 How to use the comparison",
                "value": (
                    "**Previous active:** TP5 + SL75 with PCR sizing (2.5% on PCR-flagged signals, otherwise 5%). "
                    "**Current:** TP5 + SL75 at **5%** per admitted trade, but Daily-Confirmed Core flagged or non-computable signals are **not published/traded**. "
                    "The current account replay therefore includes the real capacity benefit from freeing slots when risky signals are filtered."
                ),
                "inline": False,
            },
            {
                "name": "Today",
                "value": f"Published confirmed shorts **{report.confirmed_today}** • signals currently tracked **{report.open_count}**",
                "inline": False,
            },
        ])

        board = {
            "title": "📊 Exhaustion Scanner • Performance & Playbook",
            "description": (
                f"**{self._pretty_label(label)}** • Updated **{as_of_text}**\n"
                "Previous PCR versus the current **Daily-Confirmed Core hard-filter** strategy. The current replay changes admission as well as capacity, matching live behavior."
            ),
            "color": 0x5865F2,
            "fields": fields,
            "footer": {
                "text": (
                    "Account replay is chronological, respects capacity and includes 0.08% entry/exit fees plus current MTM. "
                    "Funding/slippage are not modeled; same-candle TP5/SL75 is conservatively stop-first."
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
        volatility_csv: bytes | None = None,
        as_of: datetime | None = None,
        timezone_name: str = "Europe/Zurich",
    ) -> bool:
        """Send research intelligence plus machine-readable evidence.

        Discord gets the decision-relevant conclusions. CSV attachments keep the
        full signal/path/feature evidence so a later LLM or human review can audit
        the conclusions without reconstructing strategy semantics.
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
            "hold_7d": report.portfolio_hold_7d,
        }
        standard_scale_5 = report.portfolio_standard_tp5_10
        standard_scale_75 = report.portfolio_standard_tp5_10x75
        standard_scale_10 = report.portfolio_standard_tp5_10x10
        standard_scale_sl75_5 = report.portfolio_standard_tp5_sl75_10
        standard_scale_sl75_10 = report.portfolio_standard_tp5_sl75_10x10
        standard_scale_summary = report.standard_tp5_validation
        standard_scale_sl75_summary = report.standard_tp5_sl75_validation
        prospective_validations = {item.strategy: item for item in report.prospective_strategy_validations}
        prospective_portfolios = dict(zip(
            ("tp5_challenger", "tp5_sl75_challenger", "hold_7d"),
            report.prospective_strategy_portfolios,
        ))
        volatility = report.volatility

        def monthly_eq(portfolio) -> float | None:
            if portfolio.replay_span_days is None or portfolio.replay_span_days <= 0:
                return None
            return portfolio.marked_return * 30.0 / portfolio.replay_span_days

        def capture_rate(portfolio) -> float | None:
            return (portfolio.entered / portfolio.eligible_signals) if portfolio.eligible_signals else None

        def exit_mix(summary) -> str:
            if summary.strategy == "hold_7d":
                return f"7D closed **{summary.timeout_exits}** • still <7D **{summary.waiting}**"
            parts = [f"TP5 **{summary.target_exits}**"]
            if summary.stop_exits:
                parts.append(f"SL75 **{summary.stop_exits}**")
            parts.append(f"open **{summary.waiting}**")
            return " • ".join(parts)

        def strategy_evidence(strategy: str, label: str) -> str:
            summary = validations[strategy]
            portfolio = portfolios[strategy]
            dd = f"-{self._percent(portfolio.max_mtm_drawdown)}" if portfolio.max_mtm_drawdown is not None else "n/a"
            return (
                f"**{label}:** {exit_mix(summary)}\n"
                f"all-signals net Σ **{self._signed_percent(summary.sum_marked_return)}** • avg **{self._signed_percent(summary.avg_marked_return)}** • "
                f"positive **{self._percent(summary.marked_positive_rate)}** | "
                f"6×5% MTM **{self._signed_percent(portfolio.marked_return)}** • 30D eq **{self._signed_percent(monthly_eq(portfolio))}*** • "
                f"DD **{dd}** • capture **{self._percent(capture_rate(portfolio))}**"
            )

        # Tail-insurance intelligence from the unrestricted TP5 signal stream.
        tp5_summary = validations["tp5_challenger"]
        tails = {item.threshold_pct: item for item in tp5_summary.tail_ladder}
        tail_lines: list[str] = []
        for threshold in (20, 50, 75, 100):
            item = tails.get(threshold)
            if item is None:
                continue
            recovery = (item.later_tp5_after_breach / item.breached_before_exit_or_mark) if item.breached_before_exit_or_mark else None
            tail_lines.append(
                f"**-{threshold}%:** {item.breached_before_exit_or_mark}/{tp5_summary.sample} ({self._percent(item.breach_rate)}) breached before exit/mark; "
                f"**{item.later_tp5_after_breach}** later reached TP5 ({self._percent(recovery)} of breaches)"
            )

        a = portfolios["tp5_challenger"]
        b_sl = portfolios["tp5_sl75_challenger"]
        c = portfolios["hold_7d"]
        sl_return_delta = b_sl.marked_return - a.marked_return
        if a.max_mtm_drawdown is not None and b_sl.max_mtm_drawdown is not None:
            sl_dd_delta = b_sl.max_mtm_drawdown - a.max_mtm_drawdown
        else:
            sl_dd_delta = None
        if sl_return_delta >= 0 and (sl_dd_delta is None or sl_dd_delta <= 0):
            sl_read = "SL75 currently improves both marked return and drawdown versus indefinite TP5."
        elif sl_dd_delta is not None and sl_dd_delta < 0:
            sl_read = (
                f"SL75 currently buys **{self._percent(abs(sl_dd_delta))}** less max drawdown at a "
                f"marked-return difference of **{self._signed_percent(sl_return_delta)}** versus indefinite TP5."
            )
        else:
            sl_read = (
                f"SL75 currently changes marked return by **{self._signed_percent(sl_return_delta)}** and has not yet reduced observed max drawdown; "
                "treat the stop as unproven insurance until more tail events mature."
            )

        hold_summary = validations["hold_7d"]
        hold_read = (
            f"Pure 7D hold has **{hold_summary.timeout_exits}** completed week-long trades; "
            f"completed positive rate **{self._percent(hold_summary.resolved_positive_rate)}**, avg 7D exit **{self._signed_percent(hold_summary.avg_exit_return)}**. "
            "This is the clean benchmark for whether the reversal has value beyond a quick +5% harvest."
        )
        stop75 = tails.get(75)
        tail_sample_read = (
            f"Only **{stop75.breached_before_exit_or_mark}** observed -75% breach events exist, so the exact SL75 threshold is still statistically thin."
            if stop75 is not None and stop75.breached_before_exit_or_mark < 10
            else "The -75% tail sample is becoming large enough for more stable threshold comparison."
        )
        default_capture = capture_rate(b_sl)
        capacity_read = (
            f"The 6-slot default currently captures **{self._percent(default_capture)}** of eligible signals. "
            "Broader coverage should be judged on return/DD, not on eliminating misses."
            if default_capture is not None and default_capture < 0.80
            else f"The 6-slot default currently captures **{self._percent(default_capture)}** of eligible signals; capacity is not yet the dominant bottleneck."
        )

        standard_tail75 = next((item for item in standard_scale_summary.tail_ladder if item.threshold_pct == 75), None)

        def standard_scale_line(label: str, portfolio) -> str:
            dd = f"-{self._percent(portfolio.max_mtm_drawdown)}" if portfolio.max_mtm_drawdown is not None else "n/a"
            return (
                f"**{label}:** MTM **{self._signed_percent(portfolio.marked_return)}** • "
                f"30D **{self._signed_percent(monthly_eq(portfolio))}*** • DD **{dd}** • "
                f"R/DD **{self._number(portfolio.return_over_max_drawdown)}** • "
                f"capture **{self._percent(capture_rate(portfolio))}**"
            )

        standard_read_parts = [
            standard_scale_line("D • STANDARD TP5 + SL75 • 10×5% / 50%", standard_scale_sl75_5),
            standard_scale_line("No-stop twin • 10×5% / 50%", standard_scale_5),
            standard_scale_line("Stress size • 10×7.5% / 75%", standard_scale_75),
            standard_scale_line("Stress size • 10×10% / 100%", standard_scale_10),
        ]
        if standard_tail75 is not None:
            standard_read_parts.append(
                f"STANDARD tail: -75% breaches **{standard_tail75.breached_before_exit_or_mark}/{standard_scale_summary.sample}**; "
                f"later TP5 **{standard_tail75.later_tp5_after_breach}**."
            )
        if standard_scale_5.marked_return == standard_scale_sl75_5.marked_return:
            standard_read_parts.append(
                "The **10×5% SL75 challenger is currently identical to its no-stop twin** because no admitted STANDARD trade hit -75% before TP5/mark; the stop therefore adds a catastrophic boundary at zero observed historical return cost."
            )
        else:
            standard_read_parts.append(
                f"10×5% +SL75 vs no-stop: MTM **{self._signed_percent(standard_scale_sl75_5.marked_return)}** vs "
                f"**{self._signed_percent(standard_scale_5.marked_return)}** • DD **-{self._percent(standard_scale_sl75_5.max_mtm_drawdown)}** vs "
                f"**-{self._percent(standard_scale_5.max_mtm_drawdown)}**."
            )
        standard_scale_read = "\n".join(standard_read_parts)

        feature_best = report.best_slices[:2]
        feature_worst = report.worst_slices[:2]
        feature_lines: list[str] = []
        if feature_best:
            feature_lines.append("**Stronger exploratory slices:** " + "; ".join(
                f"{item.feature_label} / {item.bucket} (n={item.sample}, score {item.rank_score:.1f})"
                for item in feature_best if item.rank_score is not None
            ))
        if feature_worst:
            feature_lines.append("**Weaker exploratory slices:** " + "; ".join(
                f"{item.feature_label} / {item.bucket} (n={item.sample}, score {item.rank_score:.1f})"
                for item in feature_worst if item.rank_score is not None
            ))

        all_vol_buckets = {
            item.bucket: item for item in volatility.buckets
            if item.cohort == "all_observed" and item.risk_tier == "all"
        }
        q1 = all_vol_buckets.get("Q1_low")
        q4 = all_vol_buckets.get("Q4_high")
        fixed_vol = volatility.portfolio_fixed
        normalized_vol = volatility.portfolio_normalized
        vol_lines = [
            f"Frozen ATR% anchor: p25 **{self._percent(volatility.calibration_p25)}** • median **{self._percent(volatility.calibration_median)}** • p75 **{self._percent(volatility.calibration_p75)}** "
            f"(pre-freeze n={volatility.calibration_sample}; missing current ATR={volatility.missing_atr}).",
        ]
        if q1 is not None and q4 is not None:
            vol_lines.append(
                f"Low-vol Q1: TP5 **{self._percent(q1.target_rate_to_date)}** • avg marked **{self._signed_percent(q1.avg_marked_return)}** • -50 **{self._percent(q1.breach50_rate)}** | "
                f"High-vol Q4: TP5 **{self._percent(q4.target_rate_to_date)}** • avg marked **{self._signed_percent(q4.avg_marked_return)}** • -50 **{self._percent(q4.breach50_rate)}**."
            )
        vol_lines.append(
            f"ATR-normalized 2.5–7.5% sizing, 6 slots / 30% cap: MTM **{self._signed_percent(normalized_vol.marked_return)}** • "
            f"DD **-{self._percent(normalized_vol.max_mtm_drawdown)}** • R/DD **{self._number(normalized_vol.return_over_max_drawdown)}** vs fixed 5%: "
            f"MTM **{self._signed_percent(fixed_vol.marked_return)}** • DD **-{self._percent(fixed_vol.max_mtm_drawdown)}**."
        )

        parabolic_flagged = volatility.parabolic_flagged_validation
        parabolic_unflagged = volatility.parabolic_unflagged_validation
        parabolic_book = volatility.parabolic_portfolio_de_risked
        parabolic_lines = [
            f"Frozen flag: 24h return **≥{self._percent(volatility.parabolic_return_24h_threshold)}** AND EMA20 extension **≥{volatility.parabolic_ema_distance_atr_threshold:.1f} ATR**.",
            "**PCR rule frozen 30 Aug 2026; historical results shown here are retrospective to that rule.**",
            f"Flagged **{parabolic_flagged.sample}**: TP5 **{parabolic_flagged.target_exits}** • SL75 **{parabolic_flagged.stop_exits}** • open **{parabolic_flagged.waiting}** • avg marked **{self._signed_percent(parabolic_flagged.avg_marked_return)}** | unflagged **{parabolic_unflagged.sample}**: SL75 **{parabolic_unflagged.stop_exits}**.",
            f"De-risk flagged to **{self._percent(volatility.parabolic_position_fraction)}** (others 5%), same 6 slots / 30% cap: MTM **{self._signed_percent(parabolic_book.marked_return)}** • DD **-{self._percent(parabolic_book.max_mtm_drawdown)}** • R/DD **{self._number(parabolic_book.return_over_max_drawdown)}** vs fixed 5% MTM **{self._signed_percent(fixed_vol.marked_return)}** • DD **-{self._percent(fixed_vol.max_mtm_drawdown)}**.",
        ]

        htf_flagged = volatility.htf_flagged_validation
        htf_unflagged = volatility.htf_unflagged_validation
        htf_fixed = volatility.htf_portfolio_fixed_comparable
        htf_pcr = volatility.htf_portfolio_pcr_comparable
        htf_book = volatility.htf_portfolio_de_risked
        htf_combined = volatility.htf_portfolio_pcr_plus_htf

        def htf_replay_line(label: str, book) -> str:
            dd = f"-{self._percent(book.max_mtm_drawdown)}" if book.max_mtm_drawdown is not None else "n/a"
            return (
                f"**{label}:** MTM **{self._signed_percent(book.marked_return)}** • DD **{dd}** • "
                f"R/DD **{self._number(book.return_over_max_drawdown)}** • entered **{book.entered}/{book.eligible_signals}**"
            )

        htf_lines = [
            "Frozen HTF V1: 24h ≥30% • top 2% cross-section • ≥3 ATR above 4h EMA20 • previous 1h momentum >0.",
            "**HTF V1 frozen 1 Sep 2026; replay uses one identical HTF-computable cohort for every competitor. Missing inputs are excluded from all four books, never treated as unflagged.**",
            f"Computable **{volatility.htf_computable_signals}** • missing **{volatility.htf_missing_signals}** | flagged **{htf_flagged.sample}**: TP5 **{htf_flagged.target_exits}** • SL75 **{htf_flagged.stop_exits}** • open **{htf_flagged.waiting}** | unflagged **{htf_unflagged.sample}**: SL75 **{htf_unflagged.stop_exits}**.",
            f"PCR/HTF overlap: both **{volatility.htf_pcr_both_flagged}** • HTF-only **{volatility.htf_only_flagged}** • PCR-only **{volatility.pcr_only_flagged}** • neither **{volatility.neither_flagged}**.",
            htf_replay_line("Fixed 5%", htf_fixed),
            htf_replay_line("PCR baseline", htf_pcr),
            htf_replay_line("HTF V1", htf_book),
            htf_replay_line("PCR + HTF", htf_combined),
        ]

        core_flagged = volatility.continuation_core_flagged_validation
        core_unflagged = volatility.continuation_core_unflagged_validation
        core_fixed = volatility.continuation_core_portfolio_fixed_comparable
        core_pcr = volatility.continuation_core_portfolio_pcr_comparable
        core_htf = volatility.continuation_core_portfolio_htf_comparable
        core_book = volatility.continuation_core_portfolio_de_risked
        continuation_core_lines = [
            "Frozen Continuation Core V1: run score ≥5 • ≥3 ATR above 4h EMA20 • (previous 1h momentum >0 OR top 1% cross-section).",
            "**Frozen 1 Sep 2026 after the 194-signal tail review. Research-only: no live strategy switch is added in this build. Replay uses one common computable cohort.**",
            f"Computable **{volatility.continuation_core_computable_signals}** • missing **{volatility.continuation_core_missing_signals}** | flagged **{core_flagged.sample}**: TP5 **{core_flagged.target_exits}** • SL75 **{core_flagged.stop_exits}** • open **{core_flagged.waiting}** | unflagged **{core_unflagged.sample}**: SL75 **{core_unflagged.stop_exits}**.",
            f"PCR/Core overlap: both **{volatility.continuation_core_pcr_both_flagged}** • Core-only **{volatility.continuation_core_only_flagged}** • PCR-only **{volatility.continuation_core_pcr_only_flagged}** • neither **{volatility.continuation_core_neither_flagged}**.",
            htf_replay_line("Fixed 5%", core_fixed),
            htf_replay_line("PCR baseline", core_pcr),
            htf_replay_line("HTF V1", core_htf),
            htf_replay_line("Continuation Core V1", core_book),
        ]

        def daily_tail(validation, threshold: int):
            return next((item for item in validation.tail_ladder if item.threshold_pct == threshold), None)

        daily_matrix_lines = [
            "Daily Bull V1 is structural, not threshold-fitted: last completed 1D close > EMA20D • EMA20D slope >0 • 3D momentum >0.",
            "**Frozen context layer; the current live/default strategy hard-skips Daily-Confirmed Core flagged signals.**",
            f"Computable **{volatility.daily_regime_computable_signals}** • missing **{volatility.daily_regime_missing_signals}** • 1D bullish **{volatility.daily_regime_bullish_signals}** • not bullish **{volatility.daily_regime_nonbullish_signals}**.",
        ]
        for cell in volatility.daily_core_matrix:
            validation = cell.validation
            tail50 = daily_tail(validation, 50)
            tail75 = daily_tail(validation, 75)
            daily_matrix_lines.append(
                f"**{cell.label}:** n=**{validation.sample}** • TP5 **{validation.target_exits}** • SL75 **{validation.stop_exits}** • "
                f"-50 **{tail50.breached_before_exit_or_mark if tail50 else 0}** ({self._percent(tail50.breach_rate if tail50 else None)}) • "
                f"-75 **{tail75.breached_before_exit_or_mark if tail75 else 0}** ({self._percent(tail75.breach_rate if tail75 else None)}) • "
                f"avg marked **{self._signed_percent(validation.avg_marked_return)}**"
            )

        intelligence = {
            "title": "🧠 Exhaustion Scanner • Research Intelligence",
            "description": (
                f"Updated **{display_time.strftime('%d %b %Y • %H:%M %Z')}** • observed signals **{b.total_signals}** over **{calendar.history_span_days:.1f}d**.\n"
                "A/B/C use the same STANDARD+HIGH_RISK entries and 6×5% / 30% portfolio. D is the STANDARD-only 10×5% / 50% TP5+SL75 scaling challenger."
            ),
            "color": 0x5865F2,
            "fields": [
                {
                    "name": "1 • Strategy evidence",
                    "value": "\n\n".join((
                        strategy_evidence("tp5_challenger", "A TP5 indefinite"),
                        strategy_evidence("tp5_sl75_challenger", "B TP5 + SL75"),
                        strategy_evidence("hold_7d", "C 7D hold"),
                    )),
                    "inline": False,
                },
                {
                    "name": "2 • Tail intelligence",
                    "value": "\n".join(tail_lines) if tail_lines else "No adverse-race evidence yet.",
                    "inline": False,
                },
                {
                    "name": "3 • Current read",
                    "value": (
                        f"{sl_read}\n{hold_read}\n"
                        f"TP5 timing: median **{self._hours(report.tp5_risk.median_time_hours)}**, p75 **{self._hours(report.tp5_risk.p75_time_hours)}**; "
                        f"post-freeze TP5 positions still open >7d **{report.prospective_tp5_live.waiting_over_7d}**.\n"
                        f"{tail_sample_read}\n{capacity_read}"
                    ),
                    "inline": False,
                },
                {
                    "name": "4 • STANDARD scaling challenger",
                    "value": standard_scale_read,
                    "inline": False,
                },
                {
                    "name": "5 • Entry/regime clues • exploratory",
                    "value": "\n".join(feature_lines) if feature_lines else "Not enough ranked feature evidence yet.",
                    "inline": False,
                },
                {
                    "name": "6 • Volatility / ATR risk • exploratory",
                    "value": "\n".join(vol_lines),
                    "inline": False,
                },
                {
                    "name": "7 • PCR de-risk • retrospective evidence",
                    "value": "\n".join(parabolic_lines),
                    "inline": False,
                },
                {
                    "name": "8 • HTF V1 de-risk • retrospective evidence",
                    "value": "\n".join(htf_lines),
                    "inline": False,
                },
                {
                    "name": "9 • Continuation Core V1 • retrospective evidence",
                    "value": "\n".join(continuation_core_lines),
                    "inline": False,
                },
            ],
            "footer": {
                "text": "30D eq is a linear observed run-rate, not a forecast. Feature rankings are exploratory; do not promote them to live rules without forward validation."
            },
        }

        daily_confirmed = volatility.daily_confirmed_core_portfolio_de_risked
        daily_confirmed_flagged = volatility.daily_confirmed_core_flagged_validation
        daily_confirmed_lines = [
            "Frozen Daily-Confirmed Core V1 = Continuation Core V1 AND Daily Bull V1; only the intersection is sized at 2.5%, all other computable signals stay at 5%.",
            "**Frozen 01 Sep 2026 • 23:25 CEST after the first 190-computable Core×1D matrix review. The rule is now promoted as the live hard-filter admission strategy; thresholds remain frozen.**",
            f"Computable **{volatility.daily_confirmed_core_computable_signals}** • missing **{volatility.daily_confirmed_core_missing_signals}** • flagged **{daily_confirmed_flagged.sample}**: TP5 **{daily_confirmed_flagged.target_exits}** • SL75 **{daily_confirmed_flagged.stop_exits}** • open **{daily_confirmed_flagged.waiting}**.",
            htf_replay_line("Fixed 5%", volatility.daily_confirmed_core_portfolio_fixed),
            htf_replay_line("PCR baseline", volatility.daily_confirmed_core_portfolio_pcr),
            htf_replay_line("Core V1", volatility.daily_confirmed_core_portfolio_core),
            htf_replay_line("Daily-Confirmed Core V1 • 2.5% sizing", daily_confirmed),
            htf_replay_line("Daily-Confirmed Core V1 • hard skip", volatility.daily_confirmed_core_portfolio_skip_flagged),
        ]
        hold7d_daily_fixed = volatility.hold_7d_daily_core_portfolio_fixed
        hold7d_daily_core = volatility.hold_7d_daily_core_portfolio_core
        hold7d_daily_derisk = volatility.hold_7d_daily_core_portfolio_de_risked
        hold7d_daily_skip = volatility.hold_7d_daily_core_portfolio_skip_flagged

        def hold7d_line(label: str, portfolio) -> str:
            dd = (
                f"-{self._percent(portfolio.max_mtm_drawdown)}"
                if portfolio.max_mtm_drawdown is not None else "n/a"
            )
            return (
                f"**{label}:** MTM **{self._signed_percent(portfolio.marked_return)}** • "
                f"DD **{dd}** • R/DD **{self._number(portfolio.return_over_max_drawdown)}** • "
                f"entered **{portfolio.entered}/{portfolio.eligible_signals}**"
            )

        hold7d_daily_lines = [
            "Same exact 168h exit policy as C 7D hold: no TP and no SL. Only the entry sizing/admission treatment changes.",
            f"Common daily-computable cohort **{volatility.daily_confirmed_core_computable_signals}** • Daily-Confirmed Core flagged **{daily_confirmed_flagged.sample}**.",
            hold7d_line("7D fixed 5%", hold7d_daily_fixed),
            hold7d_line("7D + Core V1 sizing", hold7d_daily_core),
            hold7d_line("7D + Daily-Confirmed Core sizing", hold7d_daily_derisk),
            hold7d_line("7D + skip Daily-Confirmed Core", hold7d_daily_skip),
        ]
        hold7d_daily_embed = {
            "title": "🗓️ 7D Hold • Daily-Confirmed Core Replay",
            "description": (
                "Research-only replay of the pure 7-day cutoff with the frozen 4h+1D continuation-risk layer. "
                "Live/default execution now uses the frozen Daily-Confirmed Core hard filter."
            ),
            "color": 0xE67E22,
            "fields": [
                {
                    "name": "7D cutoff × continuation-risk treatment",
                    "value": "\n".join(hold7d_daily_lines),
                    "inline": False,
                },
            ],
            "footer": {
                "text": "Sizing overlay = flagged 2.5%, otherwise 5%. Skip variant excludes flagged entries entirely. All use 6 slots / 30% max exposure."
            },
        }

        tp20_fixed = volatility.tp20_sl75_daily_core_portfolio_fixed
        tp20_derisk = volatility.tp20_sl75_daily_core_portfolio_de_risked
        tp20_lines = [
            "Same 6 slots / 30% cap and SL75 boundary as TP5. Only the profit target changes from +5% to +20%; Daily-Confirmed Core flags are 2.5%, others 5%.",
            f"Common daily-computable cohort **{volatility.daily_confirmed_core_computable_signals}** • flagged **{daily_confirmed_flagged.sample}**.",
            hold7d_line("TP20 + SL75 fixed 5%", tp20_fixed),
            hold7d_line("TP20 + SL75 + Daily-Confirmed Core", tp20_derisk),
            hold7d_line("TP5 + SL75 + Daily-Confirmed Core", volatility.daily_confirmed_core_portfolio_de_risked),
        ]
        tp20_embed = {
            "title": "🎯 TP20 • Daily-Confirmed Core Replay",
            "description": "Research-only larger-target replay using the frozen 4h+1D continuation-risk sizing layer. Live/default execution now uses the frozen Daily-Confirmed Core hard filter.",
            "color": 0x9B59B6,
            "fields": [{"name": "TP20 vs TP5 • same risk layer", "value": "\n".join(tp20_lines), "inline": False}],
            "footer": {"text": "TP20 and TP5 both retain SL75. This isolates target size while preserving the frozen Daily-Confirmed Core sizing rule."},
        }

        prospective_daily_flagged = volatility.prospective_daily_confirmed_core_flagged_validation
        prospective_daily_lines = [
            f"Frozen **{volatility.daily_confirmed_core_freeze_at.astimezone(tz).strftime('%d %b %Y • %H:%M %Z')}**. Only later confirmed signals count.",
            f"Computable **{volatility.prospective_daily_confirmed_core_computable_signals}** • missing **{volatility.prospective_daily_confirmed_core_missing_signals}** • flagged **{prospective_daily_flagged.sample}**: TP5 **{prospective_daily_flagged.target_exits}** • SL75 **{prospective_daily_flagged.stop_exits}** • open **{prospective_daily_flagged.waiting}**.",
            htf_replay_line("Fixed 5%", volatility.prospective_daily_confirmed_core_portfolio_fixed),
            htf_replay_line("PCR", volatility.prospective_daily_confirmed_core_portfolio_pcr),
            htf_replay_line("Core V1", volatility.prospective_daily_confirmed_core_portfolio_core),
            htf_replay_line("Daily-Confirmed Core V1", volatility.prospective_daily_confirmed_core_portfolio_de_risked),
        ]

        daily_regime_embed = {
            "title": "🗓️ 1D Regime • Core Context",
            "description": (
                "Exploratory daily-timeframe context using completed 1D candles only. "
                "No live sizing or entry rule is changed."
            ),
            "color": 0xF1C40F,
            "fields": [
                {
                    "name": "Core × 1D regime matrix",
                    "value": "\n".join(daily_matrix_lines),
                    "inline": False,
                },
                {
                    "name": "Daily-Confirmed Core V1 • retrospective replay",
                    "value": "\n".join(daily_confirmed_lines),
                    "inline": False,
                },
                {
                    "name": "Daily-Confirmed Core V1 • true forward",
                    "value": "\n".join(prospective_daily_lines),
                    "inline": False,
                },
            ],
            "footer": {
                "text": "Daily Bull V1 and Daily-Confirmed Core V1 are frozen research rules. Live/default execution now uses the frozen Daily-Confirmed Core hard filter."
            },
        }

        prospective_lines: list[str] = []
        for strategy, label in (
            ("tp5_challenger", "A TP5 indefinite"),
            ("tp5_sl75_challenger", "B TP5 + SL75"),
            ("hold_7d", "C 7D hold"),
        ):
            summary = prospective_validations[strategy]
            portfolio = prospective_portfolios[strategy]
            dd = f"-{self._percent(portfolio.max_mtm_drawdown)}" if portfolio.max_mtm_drawdown is not None else "n/a"
            prospective_lines.append(
                f"**{label}:** {exit_mix(summary)} • all-signals net Σ **{self._signed_percent(summary.sum_marked_return)}** • "
                f"portfolio MTM **{self._signed_percent(portfolio.marked_return)}** • DD **{dd}** • capture **{self._percent(capture_rate(portfolio))}**"
            )

        prospective_standard_lines = (
            standard_scale_line("D • TP5 + SL75 • 10×5% / 50%", report.prospective_portfolio_standard_tp5_sl75_10),
            standard_scale_line("No-stop twin • 10×5% / 50%", report.prospective_portfolio_standard_tp5_10),
            standard_scale_line("Stress size • 10×7.5% / 75%", report.prospective_portfolio_standard_tp5_10x75),
            standard_scale_line("Stress size • 10×10% / 100%", report.prospective_portfolio_standard_tp5_10x10),
        )

        if all(item is not None for item in (
            calendar.latest_30d_tp5, calendar.latest_30d_tp5_sl75, calendar.latest_30d_hold_7d
        )):
            thirty_day = "\n".join((
                f"A TP5 indefinite **{self._signed_percent(calendar.latest_30d_tp5.marked_return)}** / DD **-{self._percent(calendar.latest_30d_tp5.max_mtm_drawdown)}**",
                f"B TP5 + SL75 **{self._signed_percent(calendar.latest_30d_tp5_sl75.marked_return)}** / DD **-{self._percent(calendar.latest_30d_tp5_sl75.max_mtm_drawdown)}**",
                f"C 7D hold **{self._signed_percent(calendar.latest_30d_hold_7d.marked_return)}** / DD **-{self._percent(calendar.latest_30d_hold_7d.max_mtm_drawdown)}**",
            ))
        else:
            thirty_day = (
                f"True 30d empty-book comparison not available yet • observed **{calendar.history_span_days:.1f}d** • "
                f"need **{calendar.days_until_30d:.1f}d** more. Until then, use the 30D-equivalent only as a run-rate."
            )

        prospective = {
            "title": "📡 Forward Evidence • Frozen 21 Aug",
            "description": (
                f"Post-freeze only • frozen **{report.oos_freeze_at.astimezone(tz).strftime('%d %b %Y • %H:%M %Z')}**. "
                "This is the evidence that should decide whether retrospective findings survive."
            ),
            "color": 0x1ABC9C,
            "fields": [
                {"name": "Post-freeze A/B/C", "value": "\n".join(prospective_lines), "inline": False},
                {"name": "Post-freeze STANDARD scaling", "value": "\n".join(prospective_standard_lines), "inline": False},
                {
                    "name": "Post-freeze ATR-normalized sizing",
                    "value": (
                        f"Fixed 5%: MTM **{self._signed_percent(volatility.prospective_portfolio_fixed.marked_return)}** • "
                        f"DD **-{self._percent(volatility.prospective_portfolio_fixed.max_mtm_drawdown)}** | "
                        f"ATR-normalized: MTM **{self._signed_percent(volatility.prospective_portfolio_normalized.marked_return)}** • "
                        f"DD **-{self._percent(volatility.prospective_portfolio_normalized.max_mtm_drawdown)}** • "
                        f"R/DD **{self._number(volatility.prospective_portfolio_normalized.return_over_max_drawdown)}**"
                    ),
                    "inline": False,
                },
                {
                    "name": "Aug21-cohort PCR shadow • retrospective to PCR",
                    "value": (
                        "The Aug21 split predates the PCR rule; do **not** treat this as PCR out-of-sample evidence. "
                        f"Flagged signals **{volatility.prospective_parabolic_flagged_validation.sample}** • SL75 **{volatility.prospective_parabolic_flagged_validation.stop_exits}** | "
                        f"fixed 5% MTM **{self._signed_percent(volatility.prospective_portfolio_fixed.marked_return)}** / DD **-{self._percent(volatility.prospective_portfolio_fixed.max_mtm_drawdown)}** vs "
                        f"parabolic 2.5% MTM **{self._signed_percent(volatility.prospective_parabolic_portfolio_de_risked.marked_return)}** / DD **-{self._percent(volatility.prospective_parabolic_portfolio_de_risked.max_mtm_drawdown)}** / R-DD **{self._number(volatility.prospective_parabolic_portfolio_de_risked.return_over_max_drawdown)}**"
                    ),
                    "inline": False,
                },
                {
                    "name": "Continuation Core V1 • true forward since Sep 1 freeze",
                    "value": (
                        f"Frozen **{volatility.continuation_core_freeze_at.astimezone(tz).strftime('%d %b %Y • %H:%M %Z')}** after the 194-signal discovery/replay. "
                        "Only later confirmed signals count here; no Aug21 or retrospective rows are reused.\n"
                        f"Computable **{volatility.prospective_continuation_core_computable_signals}** • missing **{volatility.prospective_continuation_core_missing_signals}** | "
                        f"Core flagged **{volatility.prospective_continuation_core_flagged_validation.sample}**: TP5 **{volatility.prospective_continuation_core_flagged_validation.target_exits}** • "
                        f"SL75 **{volatility.prospective_continuation_core_flagged_validation.stop_exits}** • open **{volatility.prospective_continuation_core_flagged_validation.waiting}**.\n"
                        f"Overlap: PCR+Core **{volatility.prospective_continuation_core_pcr_both_flagged}** • Core-only **{volatility.prospective_continuation_core_only_flagged}** • "
                        f"PCR-only **{volatility.prospective_continuation_core_pcr_only_flagged}** • neither **{volatility.prospective_continuation_core_neither_flagged}**. "
                        f"Core-only outcomes: n=**{volatility.prospective_continuation_core_only_validation.sample}** • TP5 **{volatility.prospective_continuation_core_only_validation.target_exits}** • "
                        f"SL75 **{volatility.prospective_continuation_core_only_validation.stop_exits}** • open **{volatility.prospective_continuation_core_only_validation.waiting}** • "
                        f"avg marked **{self._signed_percent(volatility.prospective_continuation_core_only_validation.avg_marked_return)}**.\n"
                        f"Fixed 5%: MTM **{self._signed_percent(volatility.prospective_continuation_core_portfolio_fixed.marked_return)}** / "
                        f"DD **-{self._percent(volatility.prospective_continuation_core_portfolio_fixed.max_mtm_drawdown)}** | "
                        f"PCR: MTM **{self._signed_percent(volatility.prospective_continuation_core_portfolio_pcr.marked_return)}** / "
                        f"DD **-{self._percent(volatility.prospective_continuation_core_portfolio_pcr.max_mtm_drawdown)}** / R-DD **{self._number(volatility.prospective_continuation_core_portfolio_pcr.return_over_max_drawdown)}** | "
                        f"Core: MTM **{self._signed_percent(volatility.prospective_continuation_core_portfolio_de_risked.marked_return)}** / "
                        f"DD **-{self._percent(volatility.prospective_continuation_core_portfolio_de_risked.max_mtm_drawdown)}** / R-DD **{self._number(volatility.prospective_continuation_core_portfolio_de_risked.return_over_max_drawdown)}**"
                    ),
                    "inline": False,
                },
                {"name": "True latest-30d empty-book replay", "value": thirty_day, "inline": False},
                {
                    "name": "Research bundle",
                    "value": (
                        "**strategy-validation.csv** — decision table: all-signal economics, portfolio return/DD, 30D run-rate, capture and tails.\n"
                        "**research-signal-dataset.csv** — every signal with frozen features, full path statistics, adverse/target timestamps and explicit A/B/C outcomes.\n"
                        "**feature-lift / entry-research / token-regime / strategy-sweeps** — exploratory evidence retained for deeper LLM/human analysis.\n"
                        "**volatility-research.csv** — frozen ATR% quartiles, tier splits, ATR sizing and parabolic continuation-risk sizing evidence."
                    ),
                    "inline": False,
                },
            ],
            "footer": {
                "text": "PCR was frozen 30 Aug 2026. Continuation Core V1 true-forward evidence starts 1 Sep 2026 21:57 CEST after its 194-signal discovery replay. Funding and real execution slippage remain outside the replay."
            },
        }

        try:
            for embed in (intelligence, daily_regime_embed, hold7d_daily_embed, tp20_embed, prospective):
                self._validate_discord_embed(embed)

            payload = {
                "username": "Exhaustion Scanner • Research",
                "embeds": [intelligence],
                "allowed_mentions": {"parse": []},
            }
            files: dict[str, tuple[str, bytes, str]] = {}
            if dataset_csv is not None:
                files[f"files[{len(files)}]"] = (
                    f"research-signal-dataset-{display_time.strftime('%Y-%m-%d')}.csv", dataset_csv, "text/csv"
                )
            if strategy_csv is not None:
                files[f"files[{len(files)}]"] = (
                    f"strategy-validation-{display_time.strftime('%Y-%m-%d')}.csv", strategy_csv, "text/csv"
                )
            if feature_csv is not None:
                files[f"files[{len(files)}]"] = (
                    f"feature-lift-{display_time.strftime('%Y-%m-%d')}.csv", feature_csv, "text/csv"
                )
            if sweeps_csv is not None:
                files[f"files[{len(files)}]"] = (
                    f"strategy-sweeps-{display_time.strftime('%Y-%m-%d')}.csv", sweeps_csv, "text/csv"
                )
            if entry_csv is not None:
                files[f"files[{len(files)}]"] = (
                    f"entry-research-{display_time.strftime('%Y-%m-%d')}.csv", entry_csv, "text/csv"
                )
            if regime_csv is not None:
                files[f"files[{len(files)}]"] = (
                    f"token-regime-{display_time.strftime('%Y-%m-%d')}.csv", regime_csv, "text/csv"
                )
            if volatility_csv is not None:
                files[f"files[{len(files)}]"] = (
                    f"volatility-research-{display_time.strftime('%Y-%m-%d')}.csv", volatility_csv, "text/csv"
                )
            if files:
                response = await self._client.post(
                    self._performance_webhook_url, data={"payload_json": json.dumps(payload)}, files=files
                )
            else:
                response = await self._client.post(self._performance_webhook_url, json=payload)
            response.raise_for_status()

            response = await self._client.post(
                self._performance_webhook_url,
                json={
                    "username": "Exhaustion Scanner • Research",
                    "embeds": [daily_regime_embed],
                    "allowed_mentions": {"parse": []},
                },
            )
            response.raise_for_status()

            response = await self._client.post(
                self._performance_webhook_url,
                json={
                    "username": "Exhaustion Scanner • Research",
                    "embeds": [hold7d_daily_embed],
                    "allowed_mentions": {"parse": []},
                },
            )
            response.raise_for_status()

            response = await self._client.post(
                self._performance_webhook_url,
                json={
                    "username": "Exhaustion Scanner • Research",
                    "embeds": [tp20_embed],
                    "allowed_mentions": {"parse": []},
                },
            )
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

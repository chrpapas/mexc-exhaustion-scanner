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
from app.daily_bull_persistence_strategy import (
    DAILY_CORE_PERSISTENCE_SKIP_STRATEGY,
    DAILY_CORE_PERSISTENCE_SKIP_STRATEGY_V2,
    daily_bull_persistence_v1_missing_features,
    daily_bull_persistence_v1_state,
    daily_bull_persistence_v2_missing_features,
    daily_bull_persistence_v2_state,
)
from app.signal_ledger import SignalLedger, SignalLedgerItem
from app.signal_ledger_table import LedgerTableImage
from app.research_analytics import CurrentStrategyResearchSummary, ResearchAnalyticsReport
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
        if self._subscriber_signal_strategy in {DAILY_CORE_SKIP_STRATEGY, DAILY_CORE_PERSISTENCE_SKIP_STRATEGY, DAILY_CORE_PERSISTENCE_SKIP_STRATEGY_V2}:
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
        if self._subscriber_signal_strategy in {DAILY_CORE_PERSISTENCE_SKIP_STRATEGY, DAILY_CORE_PERSISTENCE_SKIP_STRATEGY_V2}:
            if self._subscriber_signal_strategy == DAILY_CORE_PERSISTENCE_SKIP_STRATEGY_V2:
                persistence_state = daily_bull_persistence_v2_state(features)
                missing = daily_bull_persistence_v2_missing_features(features)
                version = "V2"
            else:
                persistence_state = daily_bull_persistence_v1_state(features)
                missing = daily_bull_persistence_v1_missing_features(features)
                version = "V1"
            if persistence_state is None:
                LOGGER.warning(
                    "Subscriber signal suppressed fail-closed for %s: missing Persistence %s inputs=%s",
                    signal.symbol, version, ",".join(missing) or "unknown",
                )
                return
            if persistence_state:
                LOGGER.info(
                    "Subscriber signal hard-filtered by Trend Persistence %s: %s",
                    version, signal.symbol,
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
        """Send a lean subscriber board for the one current live strategy.

        v1.3.56 retains the lean current-strategy-only subscriber surface from the
        subscriber surface.  Research/rollback history remains in storage, but
        the public board answers one operational question only: how is the
        currently promoted Daily-Core + Persistence V2 TP5/SL75 account doing?
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

        account = report.tp5_sl75_daily_core_persistence_skip_account_run_rate

        def account_economics() -> str:
            if account is None:
                return "Current-strategy replay unavailable"
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
                f"Observed **{self._signed_percent(account.observed_account_return)}** over **{account.span_days:.1f}d** • "
                f"30D run-rate **{self._signed_percent(account.thirty_day_equivalent_return)}*** • "
                f"max DD **{dd}** • R/DD **{ratio}**\n"
                f"entered **{account.entered}/{account.eligible_signals} ({self._percent(capture)})** admitted signals • "
                f"closed/open **{account.closed}/{account.open_positions}** • "
                f"capacity/symbol misses **{account.missed_capacity}/{account.missed_same_symbol}** • "
                f"avg/peak exposure **{self._percent(account.avg_exposure_pct)} / {self._percent(account.peak_exposure_pct)}**"
            )

        board = {
            "title": "📊 Exhaustion Scanner • Performance & Playbook • Current Strategy",
            "description": (
                f"**{self._pretty_label(label)}** • Updated **{as_of_text}**\n"
                "Only the currently promoted live/default strategy is shown. Legacy PCR, HTF, Daily-Core-only, TP20 and 7D competitors are intentionally omitted."
            ),
            "color": 0x5865F2,
            "fields": [
                {
                    "name": "▶️ Live/default • Daily-Core + Persistence V2",
                    "value": (
                        "**TP5 + SL75** • STANDARD + HIGH_RISK confirmed shorts • **1× cross** • "
                        "**5% of current equity per admitted entry** • max **6** open positions / **30%** aggregate exposure • "
                        "one position per symbol • TP **+5%** • catastrophic SL **-75%** • no timeout.\n"
                        "Admission is fail-closed: skip Daily-Confirmed Core V1 flagged/non-computable signals, then skip "
                        "Trend Persistence V2 flagged/non-computable signals on its reachable branch. "
                        "V2 keeps the frozen early V1 branch and adds Mature-Run Weak-Breakdown V1: Daily Bull + Core-false + run→breakdown ≥24h + previous 1h momentum >0 + no lower-high/lower-close + no 15m structural break."
                    ),
                    "inline": False,
                },
                {
                    "name": "📈 Current account replay",
                    "value": account_economics(),
                    "inline": False,
                },
                {
                    "name": "Today",
                    "value": f"Published confirmed shorts **{report.confirmed_today}** • signals currently tracked **{report.open_count}**",
                    "inline": False,
                },
                {
                    "name": "How to read it",
                    "value": (
                        "The chronological account replay respects 6-slot capacity, one-position-per-symbol, compounding and "
                        "**0.08% fee per fill** plus current MTM. **30D run-rate*** linearly scales the observed period and is not a forecast. "
                        "Funding and real execution slippage are not modeled."
                    ),
                    "inline": False,
                },
            ],
            "footer": {
                "text": "Current strategy only • Daily-Core + Persistence V2 • 6×5% / 30% • TP5 / SL75"
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
        report: CurrentStrategyResearchSummary | ResearchAnalyticsReport,
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
        """Send a lean current-strategy research board.

        v1.3.56 retains the lean current-only research surface and removes legacy PCR/HTF/TP20/7D/exposure-challenger embeds from
        routine Discord research.  The old full ResearchAnalyticsReport is still
        accepted for backwards-compatible tests/offline callers, but only its
        current Daily-Core + Persistence V2 slice is rendered.
        """
        if not self._performance_webhook_url:
            return False

        tz = ZoneInfo(timezone_name)
        display_time = (as_of or report.generated_at).astimezone(tz)

        if isinstance(report, CurrentStrategyResearchSummary):
            total_signals = report.total_signals
            core_flagged = report.daily_core_flagged
            core_missing = report.daily_core_missing
            persistence_flagged = report.persistence_flagged
            persistence_missing = report.persistence_missing
            admitted = report.admitted_signals
            validation = report.admitted_validation
            portfolio = report.portfolio
            freeze_at = report.freeze_at
            forward_total = report.prospective_total_signals
            forward_core_flagged = report.prospective_daily_core_flagged
            forward_core_missing = report.prospective_daily_core_missing
            forward_persistence_flagged = report.prospective_persistence_flagged
            forward_persistence_missing = report.prospective_persistence_missing
            forward_admitted = report.prospective_admitted_signals
            forward_validation = report.prospective_validation
            forward_portfolio = report.prospective_portfolio
        else:
            # Backward-compatible extraction from the former full research object.
            persistence = report.volatility.daily_bull_persistence
            total_signals = report.baseline.total_signals
            daily_core = report.volatility.daily_confirmed_core_flagged_validation
            core_flagged = daily_core.sample
            core_missing = report.volatility.daily_confirmed_core_missing_signals
            persistence_flagged = persistence.flagged_validation.sample
            persistence_missing = 0
            admitted = persistence.unflagged_validation.sample
            validation = persistence.unflagged_validation
            portfolio = persistence.portfolio_skip_flagged
            freeze_at = persistence.freeze_at
            forward_total = persistence.prospective_computable_signals
            forward_core_flagged = 0
            forward_core_missing = 0
            forward_persistence_flagged = persistence.prospective_flagged_validation.sample
            forward_persistence_missing = 0
            forward_admitted = persistence.prospective_unflagged_validation.sample
            forward_validation = persistence.prospective_unflagged_validation
            forward_portfolio = persistence.prospective_portfolio_skip_flagged

        def capture(book) -> float | None:
            return (book.entered / book.eligible_signals) if book.eligible_signals else None

        def monthly(book) -> float | None:
            if book.replay_span_days is None or book.replay_span_days <= 0:
                return None
            return book.marked_return * 30.0 / book.replay_span_days

        def book_line(book) -> str:
            dd = f"-{self._percent(book.max_mtm_drawdown)}" if book.max_mtm_drawdown is not None else "n/a"
            return (
                f"MTM **{self._signed_percent(book.marked_return)}** • 30D run-rate **{self._signed_percent(monthly(book))}*** • "
                f"DD **{dd}** • R/DD **{self._number(book.return_over_max_drawdown)}**\n"
                f"entered **{book.entered}/{book.eligible_signals} ({self._percent(capture(book))})** • "
                f"closed/open **{book.closed}/{book.open_positions}** • capacity/symbol misses **{book.missed_capacity}/{book.missed_same_symbol}** • "
                f"avg/peak exposure **{self._percent(book.avg_exposure_pct)} / {self._percent(book.max_observed_exposure_pct)}**"
            )

        tails = {item.threshold_pct: item for item in validation.tail_ladder}
        tail_bits = []
        for threshold in (20, 50, 75):
            item = tails.get(threshold)
            if item is not None:
                tail_bits.append(
                    f"-{threshold}% **{item.breached_before_exit_or_mark}/{validation.sample}** "
                    f"({self._percent(item.breach_rate)}); later TP5 **{item.later_tp5_after_breach}**"
                )

        current = {
            "title": "🧠 Exhaustion Scanner • Research Intelligence • Current Strategy",
            "description": (
                f"Updated **{display_time.strftime('%d %b %Y • %H:%M %Z')}** • raw signals **{total_signals}**.\n"
                "Only the promoted **Daily-Core + Persistence V2 • TP5/SL75 • 6×5% / 30%** strategy is shown. Legacy challenger sections are no longer rendered."
            ),
            "color": 0x5865F2,
            "fields": [
                {
                    "name": "🧭 Trend Persistence • V2 • current live rule",
                    "value": (
                        "STANDARD + HIGH_RISK confirmed shorts • Daily-Confirmed Core hard skip (fail closed) • "
                        "Trend Persistence V2 hard skip (fail closed on its reachable branch) • "
                        "6 slots × 5% • 30% max exposure • TP +5% • SL -75% • no timeout.\n"
                        "V2 = early V1 branch (≤6h + extreme daily extension/acceleration) OR mature-run weak-breakdown branch (≥24h + previous 1h momentum >0 + no lower-high/lower-close + no 15m structural break)."
                    ),
                    "inline": False,
                },
                {
                    "name": "2 • Retrospective current-strategy replay",
                    "value": (
                        f"Admission: raw **{total_signals}** • Daily-Core flagged/missing **{core_flagged}/{core_missing}** • "
                        f"Persistence flagged/missing **{persistence_flagged}/{persistence_missing}** • admitted **{admitted}**.\n"
                        f"TP5 **{validation.target_exits}** • SL75 **{validation.stop_exits}** • open **{validation.waiting}**.\n"
                        f"{book_line(portfolio)}"
                    ),
                    "inline": False,
                },
                {
                    "name": "3 • Current tail profile",
                    "value": " • ".join(tail_bits) if tail_bits else "No current-strategy tail observations yet.",
                    "inline": False,
                },
                {
                    "name": "4 • True-forward evidence",
                    "value": (
                        f"Frozen **{freeze_at.astimezone(tz).strftime('%d %b %Y • %H:%M %Z')}**. "
                        f"Raw post-freeze **{forward_total}** • Daily-Core flagged/missing **{forward_core_flagged}/{forward_core_missing}** • "
                        f"Persistence flagged/missing **{forward_persistence_flagged}/{forward_persistence_missing}** • admitted **{forward_admitted}**.\n"
                        f"TP5 **{forward_validation.target_exits}** • SL75 **{forward_validation.stop_exits}** • open **{forward_validation.waiting}**.\n"
                        f"{book_line(forward_portfolio)}"
                    ),
                    "inline": False,
                },
            ],
            "footer": {
                "text": "Current strategy only • 30D is an observed linear run-rate, not a forecast • funding/slippage excluded"
            },
        }

        try:
            self._validate_discord_embed(current)
            payload = {
                "username": "Exhaustion Scanner • Research",
                "embeds": [current],
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
                    f"current-strategy-validation-{display_time.strftime('%Y-%m-%d')}.csv",
                    strategy_csv,
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
            return True
        except (httpx.HTTPError, ValueError):
            LOGGER.exception("Discord current-strategy research analytics failed")
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

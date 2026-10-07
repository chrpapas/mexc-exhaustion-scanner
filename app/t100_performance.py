from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.t100_200_strategy import STRATEGY_ID


# Frozen production-freeze anchors. These are benchmark/reference statistics only.
# Full-history funding before the MEXC public retention floor is incomplete.
FROZEN_FULL_RETURN_PCT = 1436.2
FROZEN_FULL_CAGR_PCT = 111.7
FROZEN_FULL_4Y_X = 20.1
FROZEN_FULL_TRIGGER_AWARE_DD_PCT = -59.8
FROZEN_FULL_CLOSE_DD_PCT = -60.1

FROZEN_COVERED_START = "10 Apr 2025"
FROZEN_COVERED_END = "25 Sep 2026"
FROZEN_COVERED_RETURN_PCT = 434.0
FROZEN_COVERED_CAGR_PCT = 215.4
FROZEN_COVERED_4Y_X = 98.9
FROZEN_COVERED_TRIGGER_AWARE_DD_PCT = -59.8
FROZEN_COVERED_WORST_SUBWINDOW_RETURN_PCT = 29.4


@dataclass(frozen=True, slots=True)
class T100PerformanceSummary:
    generated_at: datetime
    strategy_id: str
    started_at: datetime
    span_days: float
    starting_equity_usdt: float
    realized_equity_usdt: float
    current_equity_usdt: float
    realized_pnl_usdt: float
    unrealized_pnl_usdt: float
    total_return_pct: float
    thirty_day_equivalent_pct: float | None
    close_max_drawdown_pct: float | None
    current_gross_exposure_pct: float
    avg_gross_exposure_pct: float | None
    peak_gross_exposure_pct: float | None
    open_positions: int
    closed_positions: int
    closed_wins: int
    closed_losses: int
    closed_win_rate: float | None
    low_entries: int
    high_entries: int
    funding_net_usdt: float
    fees_usdt: float
    slippage_usdt: float
    median_adverse_pct: float | None
    worst_adverse_pct: float | None
    sl75_exits: int
    trail_exits: int
    raw_p2: int
    stage1_count: int
    eligible_stage2: int
    accepted_stage2: int
    ignored_capacity: int
    ignored_duplicate_symbol: int
    ignored_invalid: int
    ignored_no_equity: int
    today_eligible_stage2: int
    today_entries: int
    today_exits: int
    today_net_closed_pnl_usdt: float
    open_lines: tuple[str, ...]


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _close_drawdown_pct(snapshots: list[dict[str, Any]]) -> float | None:
    points = sorted(
        (
            (row.get("snapshot_at"), _as_float(row.get("equity_usdt")))
            for row in snapshots
            if row.get("snapshot_at") is not None and row.get("equity_usdt") is not None
        ),
        key=lambda item: item[0],
    )
    if not points:
        return None
    peak = points[0][1]
    worst = 0.0
    for _, equity in points:
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, (equity / peak - 1.0) * 100.0)
    return worst


def build_t100_performance_summary(
    *,
    runtime: dict[str, Any],
    positions: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    now_utc: datetime,
    timezone_name: str,
) -> T100PerformanceSummary:
    tz = ZoneInfo(timezone_name)
    local_date = now_utc.astimezone(tz).date()

    starting = _as_float(runtime.get("starting_equity_usdt"))
    realized_equity = _as_float(runtime.get("realized_equity_usdt"), starting)
    started_at = runtime.get("started_at") or now_utc
    span_days = max((now_utc - started_at).total_seconds() / 86400.0, 0.0)

    open_rows = [p for p in positions if p.get("status") == "open"]
    closed_rows = [p for p in positions if p.get("status") == "closed"]

    unrealized = sum(
        _as_float(p.get("notional_usdt"))
        * (
            _as_float(p.get("entry_price")) - _as_float(p.get("current_price"))
        )
        / _as_float(p.get("entry_price"), 1.0)
        for p in open_rows
        if _as_float(p.get("entry_price")) > 0
    )
    current_equity = realized_equity + unrealized
    total_return_pct = (
        (current_equity / starting - 1.0) * 100.0 if starting > 0 else 0.0
    )
    thirty_day_equivalent = (
        total_return_pct * (30.0 / span_days) if span_days > 0 else None
    )

    gross_notional = sum(_as_float(p.get("notional_usdt")) for p in open_rows)
    current_exposure = (
        gross_notional / current_equity * 100.0 if current_equity > 0 else 0.0
    )

    exposures = [
        _as_float(row.get("gross_exposure_pct"))
        for row in snapshots
        if row.get("gross_exposure_pct") is not None
    ]
    avg_exposure = statistics.fmean(exposures) if exposures else None
    peak_exposure = max(exposures) if exposures else None

    trade_net = []
    for p in closed_rows:
        trade_net.append(
            _as_float(p.get("net_pnl_usdt"))
            + _as_float(p.get("funding_pnl_usdt"))
            - _as_float(p.get("entry_fee_usdt"))
            - _as_float(p.get("entry_slippage_usdt"))
        )
    wins = sum(v > 0 for v in trade_net)
    losses = sum(v <= 0 for v in trade_net)
    win_rate = wins / len(trade_net) if trade_net else None

    adverse = [_as_float(p.get("mae_pct")) for p in positions if p.get("mae_pct") is not None]
    median_adverse = statistics.median(adverse) if adverse else None
    worst_adverse = max(adverse) if adverse else None

    fees = sum(
        _as_float(p.get("entry_fee_usdt")) + _as_float(p.get("exit_fee_usdt"))
        for p in positions
    )
    slippage = sum(
        _as_float(p.get("entry_slippage_usdt"))
        + _as_float(p.get("exit_slippage_usdt"))
        for p in positions
    )
    funding = sum(_as_float(p.get("funding_pnl_usdt")) for p in positions)

    def local_day(value: Any) -> Any:
        if value is None:
            return None
        return value.astimezone(tz).date()

    today_entries_rows = [p for p in positions if local_day(p.get("opened_at")) == local_date]
    today_exits_rows = [p for p in closed_rows if local_day(p.get("closed_at")) == local_date]
    today_closed_pnl = sum(
        _as_float(p.get("net_pnl_usdt"))
        + _as_float(p.get("funding_pnl_usdt"))
        - _as_float(p.get("entry_fee_usdt"))
        - _as_float(p.get("entry_slippage_usdt"))
        for p in today_exits_rows
    )

    eligible = [s for s in signals if bool(s.get("eligible_stage2"))]
    accepted = [s for s in signals if s.get("trade_decision") == "accepted"]

    open_lines = tuple(
        (
            f"#{int(p.get('slot_no') or 0)} {p.get('symbol')} • {p.get('tier')} • "
            f"{_as_float(p.get('current_return_pct')):+.2f}% • "
            f"best {_as_float(p.get('best_profit_pct')):+.2f}%"
        )
        for p in sorted(open_rows, key=lambda item: int(item.get("slot_no") or 99))
    )

    return T100PerformanceSummary(
        generated_at=now_utc,
        strategy_id=STRATEGY_ID,
        started_at=started_at,
        span_days=span_days,
        starting_equity_usdt=starting,
        realized_equity_usdt=realized_equity,
        current_equity_usdt=current_equity,
        realized_pnl_usdt=realized_equity - starting,
        unrealized_pnl_usdt=unrealized,
        total_return_pct=total_return_pct,
        thirty_day_equivalent_pct=thirty_day_equivalent,
        close_max_drawdown_pct=_close_drawdown_pct(snapshots),
        current_gross_exposure_pct=current_exposure,
        avg_gross_exposure_pct=avg_exposure,
        peak_gross_exposure_pct=peak_exposure,
        open_positions=len(open_rows),
        closed_positions=len(closed_rows),
        closed_wins=wins,
        closed_losses=losses,
        closed_win_rate=win_rate,
        low_entries=sum(p.get("tier") == "LOW_100" for p in positions),
        high_entries=sum(p.get("tier") == "HIGH_200" for p in positions),
        funding_net_usdt=funding,
        fees_usdt=fees,
        slippage_usdt=slippage,
        median_adverse_pct=median_adverse,
        worst_adverse_pct=worst_adverse,
        sl75_exits=sum(p.get("exit_reason") == "sl75" for p in closed_rows),
        trail_exits=sum(p.get("exit_reason") == "trail_gap1" for p in closed_rows),
        raw_p2=len(signals),
        stage1_count=sum(int(s.get("stage_no") or 0) == 1 for s in signals),
        eligible_stage2=len(eligible),
        accepted_stage2=len(accepted),
        ignored_capacity=sum(s.get("trade_decision") == "ignored_capacity" for s in signals),
        ignored_duplicate_symbol=sum(
            s.get("trade_decision") == "ignored_duplicate_symbol" for s in signals
        ),
        ignored_invalid=sum(s.get("trade_decision") == "ignored_invalid" for s in signals),
        ignored_no_equity=sum(s.get("trade_decision") == "ignored_no_equity" for s in signals),
        today_eligible_stage2=sum(
            local_day(s.get("p2_at")) == local_date for s in eligible
        ),
        today_entries=len(today_entries_rows),
        today_exits=len(today_exits_rows),
        today_net_closed_pnl_usdt=today_closed_pnl,
        open_lines=open_lines,
    )

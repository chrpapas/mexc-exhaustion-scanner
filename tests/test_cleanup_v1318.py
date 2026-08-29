from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.signal_ledger import build_signal_ledger, signal_ledger_csv
from app.signal_ledger_table import _COLUMNS, render_signal_ledger_tables


def _row(episode_id: int, *, tier: str = "standard", **overrides):
    confirmed = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    row = {
        "episode_id": episode_id,
        "symbol": f"T{episode_id}_USDT",
        "confirmed_at": confirmed,
        "entry_price": 1.0,
        "risk_tier": tier,
        "current_return_pct": -0.10,
        "first_profit_at": None,
        "target_5_at": None,
        "target_20_at": None,
        "target_20_path_at": None,
        "path_mae_before_target_5": None,
        "path_mae_before_target_5_at": None,
        "adverse_50_at": None,
        "adverse_100_at": None,
        "isolated_100_breach_at": None,
        "adverse_200_path_at": None,
        "adverse_200_breach_at": None,
        "adverse_300_path_at": None,
        "adverse_300_breach_at": None,
        "adverse_400_path_at": None,
        "cross_400_breach_at": None,
        "return_24h_pct": None,
        "return_48h_pct": None,
        "return_72h_pct": None,
        "return_168h_pct": None,
    }
    row.update(overrides)
    return row


def test_ledger_strategy_outcomes_use_strategy_specific_breach_cutoffs():
    confirmed = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    generated = confirmed + timedelta(days=9)
    ledger = build_signal_ledger(
        [
            _row(
                1,
                tier="standard",
                target_5_at=confirmed + timedelta(hours=4),
                adverse_50_at=confirmed + timedelta(hours=2),
                adverse_100_at=confirmed + timedelta(hours=6),
                return_168h_pct=0.30,
            ),
            _row(
                2,
                tier="high_risk",
                target_5_at=confirmed + timedelta(hours=2),
                target_20_path_at=confirmed + timedelta(hours=100),
                adverse_50_at=confirmed + timedelta(hours=10),
                adverse_100_at=confirmed + timedelta(hours=20),
                adverse_200_path_at=confirmed + timedelta(hours=120),
            ),
        ],
        generated_at=generated,
    )
    std = next(item for item in ledger.items if item.episode_id == 1)
    high = next(item for item in ledger.items if item.episode_id == 2)

    assert std.tp5_strategy.state == "target_hit"
    assert std.tp5_strategy.deepest_breach_before_effective_pct == 50
    assert std.tp5_strategy.breach_100_before_effective is False
    assert std.standard_7d_strategy.state == "closed_win"
    assert std.standard_7d_strategy.deepest_breach_before_effective_pct == 100
    # Ledger evidence is shown for TP20 on STANDARD too, even though the
    # subscriber TP20 portfolio recommendation remains HIGH_RISK-only.
    assert std.tp20_strategy.eligible is True
    assert std.tp20_strategy.state == "open"

    assert high.tp20_strategy.state == "target_hit"
    assert high.tp20_strategy.deepest_breach_before_effective_pct == 100
    assert high.tp20_strategy.breach_200_before_effective is False
    assert high.standard_7d_strategy.eligible is True
    assert high.standard_7d_strategy.state == "tracking"


def test_open_no_timeout_strategy_reports_breaches_so_far():
    confirmed = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    ledger = build_signal_ledger(
        [
            _row(
                3,
                tier="high_risk",
                current_return_pct=-0.80,
                adverse_50_at=confirmed + timedelta(hours=30),
                adverse_100_at=None,
            )
        ],
        generated_at=confirmed + timedelta(days=5),
    )
    outcome = ledger.items[0].tp20_strategy
    assert outcome.state == "open"
    assert outcome.return_pct == -0.80
    assert outcome.deepest_breach_before_effective_pct == 50
    assert outcome.breach_50_before_effective is True
    assert outcome.breach_100_before_effective is False


def test_strategy_ledger_csv_and_png_reflect_all_three_selected_strategies_and_breach_flags():
    confirmed = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    ledger = build_signal_ledger(
        [
            _row(4, tier="standard", return_168h_pct=0.25),
            _row(5, tier="high_risk", adverse_50_at=confirmed + timedelta(hours=2)),
        ],
        generated_at=confirmed + timedelta(days=8),
    )
    text = signal_ledger_csv(ledger).decode("utf-8")
    assert "tp5_breach_50_before_target_or_mark" in text
    assert "tp20_breach_100_before_target_or_mark" in text
    assert "standard_7d_breach_300_before_exit_or_mark" in text
    assert "tp20_status" in text
    assert "standard_7d_status" in text

    column_names = [name for name, _ in _COLUMNS]
    assert "TP5 FREQUENT" in column_names
    assert "TP20 NO TIMEOUT" in column_names
    assert "7D SWING" in column_names

    images = render_signal_ledger_tables(ledger, timezone_name="Europe/Zurich")
    assert len(images) == 2
    assert all(image.png_bytes.startswith(b"\x89PNG\r\n\x1a\n") for image in images)


def test_performance_rows_exposes_path_derived_subscriber_breach_thresholds():
    source = Path("app/research_path_aggregation.py").read_text(encoding="utf-8")
    for field in (
        '"adverse_50_at"',
        '"adverse_75_at"',
        '"adverse_100_at"',
        '"adverse_200_path_at"',
        '"adverse_300_path_at"',
        '"adverse_400_path_at"',
        '"path_mae_before_target_20"',
        '"path_mae_7d"',
    ):
        assert field in source

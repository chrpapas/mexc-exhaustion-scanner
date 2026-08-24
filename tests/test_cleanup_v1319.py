from datetime import UTC, datetime, timedelta

from app.signal_ledger import build_signal_ledger
from app.signal_ledger_table import _AMBER, _GREEN, _RED, _RED_TEXT, _strategy_cell


def _row(episode_id: int, *, tier: str, **overrides):
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


def test_tp5_winner_stays_green_when_breach_happened_before_target():
    confirmed = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    ledger = build_signal_ledger(
        [
            _row(
                1,
                tier="high_risk",
                target_5_at=confirmed + timedelta(hours=60),
                adverse_50_at=confirmed + timedelta(hours=3),
                adverse_100_at=confirmed + timedelta(hours=28),
            )
        ],
        generated_at=confirmed + timedelta(days=4),
    )
    cell = _strategy_cell(ledger.items[0], ledger.items[0].tp5_strategy, target_pct=5)
    assert cell.fill == _GREEN
    assert cell.main_text.startswith("HIT +5%")
    assert "-100%" in cell.detail_text
    assert cell.detail_color == _RED_TEXT


def test_open_underwater_tp5_is_amber_not_red_even_with_breach_warning():
    confirmed = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    ledger = build_signal_ledger(
        [_row(2, tier="high_risk", current_return_pct=-0.80, adverse_50_at=confirmed + timedelta(hours=4))],
        generated_at=confirmed + timedelta(days=2),
    )
    cell = _strategy_cell(ledger.items[0], ledger.items[0].tp5_strategy, target_pct=5)
    assert cell.fill == _AMBER
    assert cell.main_text.startswith("OPEN -80.0%")
    assert "so far -50%" in cell.detail_text
    assert cell.detail_color == _RED_TEXT


def test_red_primary_is_reserved_for_realized_7d_loss():
    confirmed = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    ledger = build_signal_ledger(
        [_row(3, tier="standard", return_168h_pct=-0.20)],
        generated_at=confirmed + timedelta(days=8),
    )
    cell = _strategy_cell(ledger.items[0], ledger.items[0].standard_7d_strategy)
    assert cell.fill == _RED
    assert cell.main_text == "CLOSED 7D -20.0%"

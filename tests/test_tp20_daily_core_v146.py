from datetime import UTC, datetime, timedelta

from app.research_analytics import _known_exit


def test_tp20_sl75_target_wins_when_target_precedes_stop():
    t0 = datetime(2026, 8, 1, tzinfo=UTC)
    row = {
        "confirmed_at": t0,
        "target_20_at": t0 + timedelta(hours=8),
        "target_20_path_at": t0 + timedelta(hours=8),
        "adverse_75_at": t0 + timedelta(hours=12),
    }
    exit_at, exit_return = _known_exit(row, strategy="tp20_sl75_challenger", generated_at=t0 + timedelta(days=2))
    assert exit_at == t0 + timedelta(hours=8)
    assert exit_return == 0.20


def test_tp20_sl75_stop_wins_when_stop_precedes_target():
    t0 = datetime(2026, 8, 1, tzinfo=UTC)
    row = {
        "confirmed_at": t0,
        "target_20_at": t0 + timedelta(hours=12),
        "target_20_path_at": t0 + timedelta(hours=12),
        "adverse_75_at": t0 + timedelta(hours=8),
    }
    exit_at, exit_return = _known_exit(row, strategy="tp20_sl75_challenger", generated_at=t0 + timedelta(days=2))
    assert exit_at == t0 + timedelta(hours=8)
    assert exit_return == -0.75

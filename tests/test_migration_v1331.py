from pathlib import Path


def test_tp5_sl75_exit_strategy_migration_preserves_legacy_values():
    sql = (Path(__file__).resolve().parents[1] / "migrations" / "016_tp5_sl75_exit_strategy.sql").read_text()
    expected = {
        "ratchet_5",
        "trailing_5",
        "trailing_15_floor_20",
        "fixed_time_standard",
        "tp20_or_timeout",
        "tp5_full",
        "tp5_sl75_full",
    }
    for value in expected:
        assert f"'{value}'" in sql
    assert "DROP CONSTRAINT IF EXISTS trader_positions_exit_strategy_check" in sql
    assert "ADD CONSTRAINT trader_positions_exit_strategy_check" in sql

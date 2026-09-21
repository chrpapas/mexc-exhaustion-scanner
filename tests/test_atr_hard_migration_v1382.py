from pathlib import Path


def test_atr_hard_decision_migration_extends_constraint_and_repairs_v1381_errors():
    sql = (Path(__file__).resolve().parents[1] / "migrations" / "021_atr_hard_filter_trader_decisions.sql").read_text()
    assert "trader_signal_decisions_decision_check" in sql
    assert "'ignored_atr_hard_filter'" in sql
    assert "'ignored_missing_atr_hard_filter_data'" in sql
    assert "'ignored_atr_capacity_gate'" in sql
    assert "WHERE decision='error'" in sql
    assert "ILIKE '%ignored_atr_hard_filter%'" in sql
    assert "ILIKE '%ignored_missing_atr_hard_filter_data%'" in sql

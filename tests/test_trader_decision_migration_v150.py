from pathlib import Path


def test_daily_core_decision_migration_allows_and_repairs_new_values():
    sql = (Path(__file__).resolve().parents[1] / "migrations" / "017_daily_core_trader_decisions.sql").read_text()
    assert "ignored_daily_core_filter" in sql
    assert "ignored_missing_daily_core_data" in sql
    assert "trader_signal_decisions_decision_check" in sql
    assert "WHERE decision = 'error'" in sql
    assert "ILIKE '%ignored_daily_core_filter%'" in sql
    assert "ILIKE '%ignored_missing_daily_core_data%'" in sql


def test_trader_error_alert_does_not_claim_signal_retry():
    source = (Path(__file__).resolve().parents[1] / "app" / "trader.py").read_text()
    assert "will retry automatically" not in source
    assert "this specific signal is recorded as an error if processing could not complete" in source

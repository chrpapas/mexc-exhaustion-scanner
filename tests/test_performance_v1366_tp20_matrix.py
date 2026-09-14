from app.performance import PerformanceSummary


def test_tp20_matrix_fields_exist():
    fields = PerformanceSummary.__dataclass_fields__
    for slots in (6, 8, 10, 12):
        for exposure in (50, 75, 100):
            assert f"tp20_indefinite_{slots}slots_{exposure}pct_account_run_rate" in fields

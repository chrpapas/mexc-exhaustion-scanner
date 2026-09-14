from app.performance import PerformanceSummary


def test_tp20_six_slot_exposure_curve_fields_exist():
    fields = PerformanceSummary.__dataclass_fields__
    for exposure in (50, 60, 70, 75, 80, 90, 100):
        assert f"tp20_indefinite_6slots_{exposure}pct_account_run_rate" in fields

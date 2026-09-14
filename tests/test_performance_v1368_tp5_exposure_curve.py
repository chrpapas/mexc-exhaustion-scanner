from app.performance import PerformanceSummary


def test_tp5_promoted_exposure_curve_fields_exist():
    fields = PerformanceSummary.__dataclass_fields__
    for exposure in (60, 70, 75, 80, 90, 100):
        assert f"tp5_sl100_lae10_24_q1_6slots_{exposure}pct_account_run_rate" in fields

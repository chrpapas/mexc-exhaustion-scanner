from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

def _aggregate_research_path_metrics(
    confirmed_at: datetime | None,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate one signal's persisted 15m path without expensive SQL sorts."""
    empty = {
        "path_rows": None,
        "path_rows_7d": None,
        "path_rows_14d": None,
        "path_last_at": None,
        "path_mfe_7d": None,
        "path_mae_7d": None,
        "path_mae_before_target_5": None,
        "path_mae_before_target_5_at": None,
        "path_mae_before_target_20": None,
        "path_mfe_at": None,
        "path_mae_at": None,
        "path_mfe_14d": None,
        "path_mae_14d": None,
        "path_mfe_14d_at": None,
        "path_mae_14d_at": None,
        "path_return_24h": None,
        "path_return_48h": None,
        "path_return_72h": None,
        "path_return_96h": None,
        "path_return_120h": None,
        "path_return_144h": None,
        "path_return_168h": None,
        "path_return_192h": None,
        "path_return_240h": None,
        "path_return_288h": None,
        "path_return_336h": None,
        "path_latest_return": None,
        "adverse_10_at": None,
        "adverse_20_at": None,
        "adverse_30_at": None,
        "adverse_50_at": None,
        "adverse_75_at": None,
        "adverse_100_at": None,
        "target_1_at": None,
        "target_2_at": None,
        "target_5_at": None,
        "target_10_at": None,
        "target_15_at": None,
        "target_20_path_at": None,
        "target_25_at": None,
        "target_30_at": None,
        "target_40_at": None,
    }
    if confirmed_at is None:
        return empty

    valid = [
        row for row in rows
        if row.get("candle_close_at") is not None and row["candle_close_at"] > confirmed_at
    ]
    if not valid:
        return empty
    valid.sort(key=lambda row: row["candle_close_at"])

    cutoffs = [24, 48, 72, 96, 120, 144, 168, 192, 240, 288, 336]
    cutoff_times = {hours: confirmed_at + timedelta(hours=hours) for hours in cutoffs}
    return_at: dict[int, float | None] = {hours: None for hours in cutoffs}
    target_levels = [0.01, 0.02, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40]
    target_at: dict[float, datetime | None] = {level: None for level in target_levels}
    adverse_levels = [-0.10, -0.20, -0.30, -0.50, -0.75, -1.00]
    adverse_at: dict[float, datetime | None] = {level: None for level in adverse_levels}

    for path in valid:
        at = path["candle_close_at"]
        close_ret = path.get("close_return_pct")
        favorable = path.get("favorable_return_pct")
        adverse = path.get("adverse_return_pct")
        for hours in cutoffs:
            if at <= cutoff_times[hours]:
                return_at[hours] = close_ret
        if favorable is not None:
            for level in target_levels:
                if target_at[level] is None and favorable >= level:
                    target_at[level] = at
        if adverse is not None:
            for level in adverse_levels:
                if adverse_at[level] is None and adverse <= level:
                    adverse_at[level] = at

    seven_day = [row for row in valid if row["candle_close_at"] <= cutoff_times[168]]
    fourteen_day = [row for row in valid if row["candle_close_at"] <= cutoff_times[336]]

    def _extreme(
        subset: list[dict[str, Any]],
        key: str,
        *,
        choose_max: bool,
    ) -> tuple[float | None, datetime | None]:
        candidates = [row for row in subset if row.get(key) is not None]
        if not candidates:
            return None, None
        if choose_max:
            value = max(float(row[key]) for row in candidates)
        else:
            value = min(float(row[key]) for row in candidates)
        at = min(row["candle_close_at"] for row in candidates if float(row[key]) == value)
        return value, at

    mfe_7d, mfe_7d_at = _extreme(seven_day, "favorable_return_pct", choose_max=True)
    mae_7d, mae_7d_at = _extreme(seven_day, "adverse_return_pct", choose_max=False)
    mfe_14d, mfe_14d_at = _extreme(fourteen_day, "favorable_return_pct", choose_max=True)
    mae_14d, mae_14d_at = _extreme(fourteen_day, "adverse_return_pct", choose_max=False)

    target5 = target_at[0.05]
    before_target5 = [
        row for row in valid
        if target5 is not None and row["candle_close_at"] < target5
    ]
    mae_before_5, mae_before_5_at = _extreme(
        before_target5, "adverse_return_pct", choose_max=False
    )

    target20 = target_at[0.20]
    through_target20 = [
        row for row in valid
        if target20 is not None and row["candle_close_at"] <= target20
    ]
    mae_before_20, _ = _extreme(through_target20, "adverse_return_pct", choose_max=False)

    return {
        "path_rows": len(valid),
        "path_rows_7d": len(seven_day),
        "path_rows_14d": len(fourteen_day),
        "path_last_at": valid[-1]["candle_close_at"],
        "path_mfe_7d": mfe_7d,
        "path_mae_7d": mae_7d,
        "path_mae_before_target_5": mae_before_5,
        "path_mae_before_target_5_at": mae_before_5_at,
        "path_mae_before_target_20": mae_before_20,
        "path_mfe_at": mfe_7d_at,
        "path_mae_at": mae_7d_at,
        "path_mfe_14d": mfe_14d,
        "path_mae_14d": mae_14d,
        "path_mfe_14d_at": mfe_14d_at,
        "path_mae_14d_at": mae_14d_at,
        **{f"path_return_{hours}h": return_at[hours] for hours in cutoffs},
        "path_latest_return": valid[-1].get("close_return_pct"),
        "adverse_10_at": adverse_at[-0.10],
        "adverse_20_at": adverse_at[-0.20],
        "adverse_30_at": adverse_at[-0.30],
        "adverse_50_at": adverse_at[-0.50],
        "adverse_75_at": adverse_at[-0.75],
        "adverse_100_at": adverse_at[-1.00],
        "target_1_at": target_at[0.01],
        "target_2_at": target_at[0.02],
        "target_5_at": target_at[0.05],
        "target_10_at": target_at[0.10],
        "target_15_at": target_at[0.15],
        "target_20_path_at": target_at[0.20],
        "target_25_at": target_at[0.25],
        "target_30_at": target_at[0.30],
        "target_40_at": target_at[0.40],
    }




def _aggregate_performance_path_metrics(
    confirmed_at: datetime | None,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate path fields required by performance/report/ledger workflows."""
    empty = {
        "target_5_at": None,
        "path_mae_before_target_5": None,
        "path_mae_before_target_5_at": None,
        "path_mae_before_target_20": None,
        "path_mae_7d": None,
        "target_20_path_at": None,
        "adverse_50_at": None,
        "adverse_75_at": None,
        "adverse_100_at": None,
        "adverse_200_path_at": None,
        "adverse_300_path_at": None,
        "adverse_400_path_at": None,
        "path_last_at": None,
        "path_rows_10d": None,
        "path_return_24h": None,
        "path_return_48h": None,
        "path_return_72h": None,
        "path_return_96h": None,
        "path_return_120h": None,
        "path_return_168h": None,
        "path_return_240h": None,
        "path_times": None,
        "path_returns": None,
    }
    if confirmed_at is None:
        return empty

    valid = [
        row for row in rows
        if row.get("candle_close_at") is not None and row["candle_close_at"] > confirmed_at
    ]
    if not valid:
        return empty
    valid.sort(key=lambda row: row["candle_close_at"])

    target_5_at = next(
        (row["candle_close_at"] for row in valid
         if row.get("favorable_return_pct") is not None and row["favorable_return_pct"] >= 0.05),
        None,
    )
    target_20_at = next(
        (row["candle_close_at"] for row in valid
         if row.get("favorable_return_pct") is not None and row["favorable_return_pct"] >= 0.20),
        None,
    )

    def first_adverse(level: float) -> datetime | None:
        return next(
            (row["candle_close_at"] for row in valid
             if row.get("adverse_return_pct") is not None and row["adverse_return_pct"] <= level),
            None,
        )

    def min_adverse(subset: list[dict[str, Any]]) -> tuple[float | None, datetime | None]:
        candidates = [row for row in subset if row.get("adverse_return_pct") is not None]
        if not candidates:
            return None, None
        value = min(float(row["adverse_return_pct"]) for row in candidates)
        at = min(
            row["candle_close_at"]
            for row in candidates
            if float(row["adverse_return_pct"]) == value
        )
        return value, at

    before_5 = [row for row in valid if target_5_at is not None and row["candle_close_at"] < target_5_at]
    mae_before_5, mae_before_5_at = min_adverse(before_5)
    before_20 = [row for row in valid if target_20_at is not None and row["candle_close_at"] < target_20_at]
    mae_before_20, _ = min_adverse(before_20)

    cutoff_7d = confirmed_at + timedelta(hours=168)
    cutoff_10d = confirmed_at + timedelta(hours=240)
    mae_7d, _ = min_adverse([row for row in valid if row["candle_close_at"] <= cutoff_7d])

    returns: dict[int, float | None] = {h: None for h in (24, 48, 72, 96, 120, 168, 240)}
    for row in valid:
        at = row["candle_close_at"]
        for hours in returns:
            if at <= confirmed_at + timedelta(hours=hours):
                returns[hours] = row.get("close_return_pct")

    return {
        "target_5_at": target_5_at,
        "path_mae_before_target_5": mae_before_5,
        "path_mae_before_target_5_at": mae_before_5_at,
        "path_mae_before_target_20": mae_before_20,
        "path_mae_7d": mae_7d,
        "target_20_path_at": target_20_at,
        "adverse_50_at": first_adverse(-0.50),
        "adverse_75_at": first_adverse(-0.75),
        "adverse_100_at": first_adverse(-1.00),
        "adverse_200_path_at": first_adverse(-2.00),
        "adverse_300_path_at": first_adverse(-3.00),
        "adverse_400_path_at": first_adverse(-4.00),
        "path_last_at": valid[-1]["candle_close_at"],
        "path_rows_10d": sum(1 for row in valid if row["candle_close_at"] <= cutoff_10d),
        "path_return_24h": returns[24],
        "path_return_48h": returns[48],
        "path_return_72h": returns[72],
        "path_return_96h": returns[96],
        "path_return_120h": returns[120],
        "path_return_168h": returns[168],
        "path_return_240h": returns[240],
        "path_times": [row["candle_close_at"] for row in valid],
        "path_returns": [row.get("close_return_pct") for row in valid],
    }

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from app.historical_live_store import SpreadProxyModel


@dataclass(frozen=True, slots=True)
class ValidationMetrics:
    matched: int
    reference: int
    replay: int
    recall: float
    precision: float
    risk_agreement: float
    median_abs_price_diff_pct: float | None
    median_abs_return_diff_pct_points: float | None
    median_abs_amount_diff_pct: float | None
    gate_pass: bool
    fail_reasons: tuple[str, ...]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    values.sort()
    n = len(values)
    if n % 2:
        return values[n // 2]
    return (values[n // 2 - 1] + values[n // 2]) / 2.0


def _bucket_5m(text: str) -> str:
    # SQLite ISO timestamps are lexically sortable. Normalize seconds to a 5m
    # floor without requiring database-specific date extensions.
    from datetime import datetime, UTC
    d = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    minute = d.minute - d.minute % 5
    return d.replace(minute=minute, second=0, microsecond=0).isoformat(sep=" ")


def validate_ticker_store(
    reference_db: Path,
    replay_db: Path,
    *,
    min_recall: float = 0.95,
    min_precision: float = 0.95,
    min_risk_agreement: float = 0.98,
    max_median_price_diff_pct: float = 0.25,
) -> ValidationMetrics:
    ref = sqlite3.connect(reference_db)
    rep = sqlite3.connect(replay_db)
    model = SpreadProxyModel()
    try:
        ref_rows = ref.execute(
            "SELECT symbol, observed_at, last_price, amount24, rise_fall_rate, spread_pct FROM ticker_snapshots"
        )
        reference: dict[tuple[str, str], tuple[float, float, float, float | None]] = {}
        for symbol, at, price, amount, ret, spread in ref_rows:
            if price is None or amount is None or ret is None:
                continue
            reference[(str(symbol), _bucket_5m(str(at)))] = (
                float(price), float(amount), float(ret), float(spread) if spread is not None else None
            )
        rep_rows = rep.execute(
            "SELECT symbol, observed_at, last_price, amount24, rise_fall_rate, spread_pct FROM ticker_snapshots"
        )
        replay: dict[tuple[str, str], tuple[float, float, float, float | None]] = {}
        for symbol, at, price, amount, ret, spread in rep_rows:
            if price is None or amount is None or ret is None:
                continue
            replay[(str(symbol), _bucket_5m(str(at)))] = (
                float(price), float(amount), float(ret), float(spread) if spread is not None else None
            )
    finally:
        ref.close(); rep.close()

    keys = set(reference) & set(replay)
    price_diffs: list[float] = []
    return_diffs: list[float] = []
    amount_diffs: list[float] = []
    risk_hits = 0
    risk_total = 0
    for key in keys:
        rp, ra, rr, rs = reference[key]
        hp, ha, hr, hs = replay[key]
        if rp > 0:
            price_diffs.append(abs(hp / rp - 1.0) * 100.0)
        return_diffs.append(abs(hr - rr) * 100.0)
        if ra > 0:
            amount_diffs.append(abs(ha / ra - 1.0) * 100.0)
        if rs is not None:
            risk_total += 1
            if model.tier(ra, rs) == model.tier(ha, hs):
                risk_hits += 1

    reference_n = len(reference)
    replay_n = len(replay)
    matched = len(keys)
    recall = matched / reference_n if reference_n else 0.0
    precision = matched / replay_n if replay_n else 0.0
    risk_agreement = risk_hits / risk_total if risk_total else 0.0
    price_med = _median(price_diffs)
    reasons: list[str] = []
    if recall < min_recall:
        reasons.append(f"ticker recall {recall:.3%} < {min_recall:.3%}")
    if precision < min_precision:
        reasons.append(f"ticker precision {precision:.3%} < {min_precision:.3%}")
    if risk_agreement < min_risk_agreement:
        reasons.append(f"risk agreement {risk_agreement:.3%} < {min_risk_agreement:.3%}")
    if price_med is None or price_med > max_median_price_diff_pct:
        reasons.append(f"median price diff {price_med} > {max_median_price_diff_pct}%")
    return ValidationMetrics(
        matched=matched,
        reference=reference_n,
        replay=replay_n,
        recall=recall,
        precision=precision,
        risk_agreement=risk_agreement,
        median_abs_price_diff_pct=price_med,
        median_abs_return_diff_pct_points=_median(return_diffs),
        median_abs_amount_diff_pct=_median(amount_diffs),
        gate_pass=not reasons,
        fail_reasons=tuple(reasons),
    )


def main() -> None:
    p = argparse.ArgumentParser(description="Validate reconstructed historical ticker store against production ticker snapshots")
    p.add_argument("--reference-db", required=True, type=Path)
    p.add_argument("--replay-db", required=True, type=Path)
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    metrics = validate_ticker_store(args.reference_db, args.replay_db)
    payload = asdict(metrics)
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print("VALIDATION:", "PASS" if metrics.gate_pass else "FAIL")
    print(text)
    raise SystemExit(0 if metrics.gate_pass else 2)


if __name__ == "__main__":
    main()

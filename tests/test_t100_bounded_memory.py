import asyncio
from datetime import UTC, datetime

import app.t100_200_worker as worker_module
from app.t100_200_worker import T100Worker


def test_bounded_candidate_selection_preserves_frozen_ranking_inputs(monkeypatch):
    worker = object.__new__(T100Worker)
    worker.request_concurrency = 2
    worker.contracts = {"BTC_USDT", "A_USDT", "B_USDT", "C_USDT"}

    rows = {
        "BTC_USDT": {"r24": 0.05, "r72": 0.10, "amount24": 200.0},
        "A_USDT": {"r24": 0.20, "r72": 0.25, "amount24": 200.0},
        "B_USDT": {"r24": 0.01, "r72": 0.01, "amount24": 60.0},
        "C_USDT": {"r24": 0.15, "r72": 0.05, "amount24": 40.0},
    }
    calls = []

    async def feature(symbol, eval_at, *, include_recent_m30=True):
        calls.append((symbol, include_recent_m30))
        row = {
            "symbol": symbol,
            "eval_at": eval_at,
            "feature_at": eval_at,
            **rows[symbol],
        }
        if include_recent_m30:
            row["recent_m30"] = [object()] * 220
        return row

    async def active_symbols():
        return {"B_USDT"}

    worker._feature_for_symbol = feature
    worker._active_scanner_symbols = active_symbols

    monkeypatch.setattr(worker_module, "EXCLUDED_SYMBOLS", set())
    monkeypatch.setattr(worker_module, "MIN_AMOUNT_24H", 100.0)
    monkeypatch.setattr(worker_module, "DISCOVERY_MIN_RETURN_24H", 0.10)
    monkeypatch.setattr(worker_module, "DISCOVERY_MIN_CROSS_SECTION_PERCENTILE", 0.50)
    monkeypatch.setattr(worker_module, "WIDE_SCAN_MIN_RETURN_72H", 0.20)
    monkeypatch.setattr(worker_module, "MAX_SYMBOLS", 3)
    monkeypatch.setattr(worker_module, "HIGH_RISK_MIN_AMOUNT_24H", 50.0)

    candidates = asyncio.run(
        worker._select_eval_candidates(datetime(2026, 10, 8, 4, 0, tzinfo=UTC))
    )

    # Active symbols retain highest selection priority, then the frozen wide-mover
    # priority. C is selected into the top three but removed by the unchanged
    # HIGH_RISK_MIN_AMOUNT_24H post-ranking filter.
    assert [row["symbol"] for row in candidates] == ["B_USDT", "A_USDT"]
    assert all("recent_m30" not in row for row in candidates)
    assert len(calls) == len(worker.contracts)
    assert all(include_recent is False for _, include_recent in calls)

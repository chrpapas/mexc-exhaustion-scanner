from pathlib import Path
import json
from app.historical_backtest import _ema_series, entry_quality, readiness


def test_entry_quality_missing_premium_is_conservative_zero():
    f={"exhaustion_score":5,"run_score":4,"amount_24h":20_000_000,"volume_zscore_15m":-0.5,"return_24h":0.30,"return_72h":0.70,"momentum_1h":-0.05}
    assert entry_quality(f)==9


def test_ema_series_matches_seed_then_recursion():
    out=_ema_series([1,2,3,4,5],3)
    assert out[2]==2
    assert out[3]==3
    assert out[4]==4


def test_readiness_requires_completed_pipeline(tmp_path:Path):
    (tmp_path/"research-window.json").write_text(json.dumps({"start":"2026-03-01T00:00:00+00:00","end":"2026-09-01T00:00:00+00:00"}))
    r=readiness(tmp_path)
    assert r["ready"] is False
    assert "historical_bid_ask_spread" in r["fidelity"]

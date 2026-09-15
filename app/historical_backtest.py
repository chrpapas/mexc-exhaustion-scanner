from __future__ import annotations

import argparse
import bisect
import collections
import functools
import gzip
import json
import logging
import math
import os
import sqlite3
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from app.daily_bull_persistence_strategy import daily_bull_persistence_v2_state
from app.daily_core_strategy import daily_confirmed_core_v1_state
from app.daily_regime import reconstruct_daily_regime_features
from app.indicators import atr, close_location, ema, pct_return, percentile_rank, upper_wick_ratio
from app.mexc import parse_klines
from app.models import Candle
from app.signals import (
    ExhaustionFeatures,
    ExhaustionThresholds,
    MarketStateThresholds,
    RunFeatures,
    RunThresholds,
    armed_runner_exhaustion_ready,
    classify_market_state,
    evaluate_failed_retest,
    score_exhaustion,
    score_run,
)

LOGGER = logging.getLogger(__name__)
FEE_PER_FILL = 0.0008
SCHEMA_VERSION = 2

# Frozen live defaults. This module deliberately does not import app.config,
# app.db, app.trader_db or app.trader and therefore never needs DATABASE_URL.
STATE_MIN_RUN_SCORE = 3
SHORT_EXHAUSTION_SCORE = 3
RETEST_WINDOW_CANDLES = 6
RETEST_TOLERANCE_ATR = 0.5
REARM_NEW_HIGH_PCT = 0.05
ARMED_RUNNER_MEMORY_HOURS = 48
CONFIRMED_REARM_HOURS = 48
EPISODE_MAX_AGE_HOURS = 240
MIN_AMOUNT_24H = 3_000_000.0
HIGH_RISK_MIN_AMOUNT_24H = 500_000.0
MAX_SYMBOLS = 400
DISCOVERY_MIN_RETURN_24H = 0.05
DISCOVERY_MIN_CROSS_SECTION_PERCENTILE = 0.70
WIDE_SCAN_MIN_RETURN_72H = 0.20
EXCLUDED_SYMBOLS = {"BTC_USDT", "ETH_USDT"}

RUN_THRESHOLDS = RunThresholds()
EXHAUSTION_THRESHOLDS = ExhaustionThresholds()
STATE_THRESHOLDS = MarketStateThresholds(
    min_run_score=STATE_MIN_RUN_SCORE,
    run_watch_min_24h=0.08,
    run_watch_min_72h=0.20,
    exhaustion_watch_min_72h=0.30,
    exhaustion_watch_min_24h=-0.25,
    exhaustion_watch_max_24h=0.08,
    active_exhaustion_min_score=2,
)


@dataclass(slots=True)
class Episode:
    symbol: str
    started_at: datetime
    state: str
    peak_price: float
    peak_at: datetime
    run_score: int
    exhaustion_score: int
    broken_level: float | None = None
    breakdown_at: datetime | None = None
    breakdown_atr_15m: float | None = None
    confirmed_short_at: datetime | None = None


@dataclass(slots=True)
class ReconstructedSignal:
    symbol: str
    confirmed_at: datetime
    entry_price: float
    risk_tier: str
    features: dict[str, Any]


@dataclass(slots=True)
class Position:
    symbol: str
    entry_at: datetime
    entry_price: float
    notional: float
    fraction: float
    quality: int
    exit_at: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None


@dataclass(slots=True)
class ReplayResult:
    exposure_pct: float
    slots: int
    total_return_pct: float
    monthly_geometric_pct: float | None
    linear_30d_pct: float | None
    max_drawdown_pct: float
    entered: int
    closed_wins: int
    closed_losses: int
    open_positions: int
    capacity_misses: int
    same_symbol_misses: int
    eligible_signals: int
    avg_exposure_pct: float
    peak_exposure_pct: float
    worst_open_mark_pct: float | None
    final_equity: float


@dataclass(slots=True)
class ReconstructionStats:
    symbols_prepared: int = 0
    feature_rows: int = 0
    evaluation_points: int = 0
    confirmed_signals: int = 0
    admitted_after_filters: int = 0
    daily_core_skips: int = 0
    persistence_skips: int = 0
    missing_daily_core: int = 0
    missing_persistence: int = 0
    amount_proxy_suppressed: int = 0
    fidelity_notes: list[str] = field(default_factory=list)


def _dt(ts: int | float) -> datetime:
    return datetime.fromtimestamp(int(ts), UTC)


def _ts(value: datetime) -> int:
    return int(value.timestamp())


def _parse_iso(value: str) -> datetime:
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC).replace(microsecond=0)


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    tmp = Path(temp)
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _load_window(cache_dir: Path) -> tuple[datetime, datetime]:
    path = cache_dir / "research-window.json"
    if not path.exists():
        raise RuntimeError("research-window.json missing; run historical collection first")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return _parse_iso(str(raw["start"])), _parse_iso(str(raw["end"]))


def readiness(cache_dir: Path) -> dict[str, Any]:
    final = cache_dir / "historical-pipeline-final.json"
    pipeline = cache_dir / "historical-pipeline-state.json"
    state: dict[str, Any] = {}
    if pipeline.exists():
        try:
            state = json.loads(pipeline.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    complete = bool(state.get("completed")) and state.get("stage") == "complete" and final.exists()
    required = ["Min15", "Min60", "Hour4", "Day1"]
    present = {}
    for interval in required:
        root = cache_dir / "candles" / interval
        present[interval] = root.exists() and any(root.iterdir())
    return {
        "ready": complete and all(present.values()),
        "pipeline_complete": complete,
        "pipeline_state": state,
        "intervals_present": present,
        "fidelity": {
            "historical_bid_ask_spread": "unavailable in OHLC archive; amount-only execution-risk proxy is used",
            "historical_fair_index_premium": "unavailable in OHLC archive; missing premium contributes zero to LAE-Q1 quality",
            "signal_evaluation_cadence": "15m completed-candle cadence; production evaluates every 5m with live ticker price",
            "intraday_indicator_window": "15m EMA/ATR use continuous causal history; production recalculates from its latest 400 candles; decay makes the difference small but non-zero",
            "tp_sl_path": "15m OHLC threshold crossing; adverse exit wins ties when TP and adverse threshold occur in one candle",
            "historical_universe": "active plus reconstructed delisted crypto futures when pipeline stage 2 is complete",
        },
    }


def _open_db(cache_dir: Path) -> sqlite3.Connection:
    path = cache_dir / "backtest" / "historical-backtest.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=60)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA cache_size=-131072")  # ~128MB page cache
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS prepared_symbols(symbol TEXT PRIMARY KEY, rows INTEGER NOT NULL, completed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS local_features(
            ts INTEGER NOT NULL, symbol TEXT NOT NULL,
            current REAL NOT NULL, r24 REAL NOT NULL, r72 REAL NOT NULL,
            amount24 REAL NOT NULL, volume_z REAL, distance REAL, atr15 REAL,
            prior_support REAL NOT NULL, peak_high REAL NOT NULL, peak_ts INTEGER NOT NULL,
            latest_open REAL NOT NULL, latest_high REAL NOT NULL, latest_low REAL NOT NULL, latest_close REAL NOT NULL,
            prev_high REAL NOT NULL, prev_close REAL NOT NULL,
            momentum REAL, prev_momentum REAL,
            upper_wick REAL, close_loc REAL,
            below_ema9 INTEGER NOT NULL, lower_high_close INTEGER NOT NULL, structural_break INTEGER NOT NULL,
            exhaustion_score INTEGER NOT NULL,
            PRIMARY KEY(ts, symbol)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS idx_local_features_symbol_ts ON local_features(symbol, ts);
        CREATE TABLE IF NOT EXISTS reconstructed_signals(
            symbol TEXT NOT NULL, confirmed_ts INTEGER NOT NULL, entry_price REAL NOT NULL,
            risk_tier TEXT NOT NULL, features_json TEXT NOT NULL,
            PRIMARY KEY(symbol, confirmed_ts)
        ) WITHOUT ROWID;
        """
    )
    return db


def _chunk_candles(cache_dir: Path, symbol: str, interval: str) -> list[Candle]:
    root = cache_dir / "candles" / interval / symbol
    if not root.exists():
        return []
    rows: dict[datetime, Candle] = {}
    for path in sorted(root.glob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as h:
            payload = json.load(h)
        for candle in parse_klines(symbol, interval, payload.get("data", {})):
            rows[candle.open_time] = candle
    return [rows[k] for k in sorted(rows)]


def _ema_series(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1.0)
    cur = sum(values[:period]) / period
    out[period - 1] = cur
    for i in range(period, len(values)):
        cur = alpha * values[i] + (1.0 - alpha) * cur
        out[i] = cur
    return out


def _atr_series(candles: list[Candle], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(candles)
    if len(candles) < period + 1:
        return out
    trs = [
        max(candles[i].high - candles[i].low, abs(candles[i].high - candles[i-1].close), abs(candles[i].low - candles[i-1].close))
        for i in range(1, len(candles))
    ]
    cur = sum(trs[:period]) / period
    out[period] = cur
    for tr_i in range(period, len(trs)):
        cur = ((period - 1) * cur + trs[tr_i]) / period
        out[tr_i + 1] = cur
    return out


def _volume_z_previous(values: list[float], lookback: int = 96) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) <= lookback:
        return out
    total = sum(values[:lookback])
    total2 = sum(v*v for v in values[:lookback])
    for i in range(lookback, len(values)):
        mean = total / lookback
        var = max(0.0, total2 / lookback - mean*mean)
        sd = math.sqrt(var)
        cur = values[i]
        if sd == 0:
            out[i] = 0.0 if cur == mean else (10.0 if cur > mean else -10.0)
        else:
            out[i] = (cur - mean) / sd
        old = values[i-lookback]
        total += cur - old
        total2 += cur*cur - old*old
    return out


def _rolling_sum(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < window:
        return out
    total = sum(values[:window])
    out[window-1] = total
    for i in range(window, len(values)):
        total += values[i] - values[i-window]
        out[i] = total
    return out


def _rolling_max_high(candles: list[Candle], window: int) -> tuple[list[float | None], list[int | None]]:
    vals: list[float | None] = [None] * len(candles)
    idxs: list[int | None] = [None] * len(candles)
    dq: collections.deque[int] = collections.deque()
    for i, c in enumerate(candles):
        while dq and dq[0] <= i-window:
            dq.popleft()
        while dq and candles[dq[-1]].high <= c.high:
            dq.pop()
        dq.append(i)
        if i >= window-1:
            vals[i] = candles[dq[0]].high
            idxs[i] = dq[0]
    return vals, idxs


def _prepare_symbol_rows(cache_dir: Path, symbol: str) -> list[tuple[Any, ...]]:
    c15 = _chunk_candles(cache_dir, symbol, "Min15")
    c4 = _chunk_candles(cache_dir, symbol, "Hour4")
    if len(c15) < 289 or len(c4) < 25:
        return []
    closes = [c.close for c in c15]
    vols = [c.volume for c in c15]
    amounts = [c.amount for c in c15]
    ema9 = _ema_series(closes, 9)
    atr15 = _atr_series(c15, 14)
    vz = _volume_z_previous(vols, 96)
    amount24 = _rolling_sum(amounts, 96)
    peak_high, peak_idx = _rolling_max_high(c15, 289)

    # Production fetches at most the latest 80 completed 4h candles for each
    # evaluation, so freeze those metrics with the same rolling-80 semantics.
    ema20_4: list[float | None] = [None] * len(c4)
    atr4: list[float | None] = [None] * len(c4)
    for j in range(len(c4)):
        window = c4[max(0, j - 79): j + 1]
        closes4 = [c.close for c in window]
        highs4 = [c.high for c in window]
        lows4 = [c.low for c in window]
        ema20_4[j] = ema(closes4, 20)
        atr4[j] = atr(highs4, lows4, closes4, 14)
    four_complete_ts = [_ts(c.open_time + timedelta(hours=4)) for c in c4]

    rows: list[tuple[Any, ...]] = []
    for i in range(288, len(c15)):
        eval_at = c15[i].open_time + timedelta(minutes=15)
        four_i = bisect.bisect_right(four_complete_ts, _ts(eval_at)) - 1
        if four_i < 24 or ema20_4[four_i] is None or atr4[four_i] is None or atr4[four_i] <= 0:
            continue
        r24 = pct_return(c15[i-96].close, c15[i].close)
        r72 = pct_return(c15[i-288].close, c15[i].close)
        if r24 is None or r72 is None or amount24[i] is None or peak_high[i] is None or peak_idx[i] is None:
            continue
        dist = (c15[i].close - float(ema20_4[four_i])) / float(atr4[four_i])
        mom = pct_return(c15[i-4].close, c15[i].close)
        prev_mom = pct_return(c15[i-8].close, c15[i-4].close)
        prior_support = min(x.low for x in c15[i-4:i])
        structural = c15[i].close < prior_support
        lower_hc = c15[i].high < c15[i-1].high and c15[i].close < c15[i-1].close
        ex = ExhaustionFeatures(
            upper_wick_ratio_15m=upper_wick_ratio(c15[i].open, c15[i].high, c15[i].low, c15[i].close),
            close_location_15m=close_location(c15[i].high, c15[i].low, c15[i].close),
            momentum_1h=mom,
            previous_momentum_1h=prev_mom,
            momentum_decelerating=(mom is not None and prev_mom is not None and mom < prev_mom),
            below_ema9_15m=ema9[i] is not None and c15[i].close < float(ema9[i]),
            lower_high_and_close=lower_hc,
            structural_break_15m=structural,
            volume_zscore_15m=vz[i],
        )
        ex_score, _ = score_exhaustion(ex, EXHAUSTION_THRESHOLDS)
        pidx = int(peak_idx[i])
        rows.append((
            _ts(eval_at), symbol, c15[i].close, r24, r72, float(amount24[i]), vz[i], dist, atr15[i],
            prior_support, float(peak_high[i]), _ts(c15[pidx].open_time),
            c15[i].open, c15[i].high, c15[i].low, c15[i].close, c15[i-1].high, c15[i-1].close,
            mom, prev_mom, ex.upper_wick_ratio_15m, ex.close_location_15m,
            int(ex.below_ema9_15m), int(lower_hc), int(structural), ex_score,
        ))
    return rows


def prepare_features(cache_dir: Path, *, force: bool = False) -> dict[str, Any]:
    ready = readiness(cache_dir)
    if not ready["ready"]:
        raise RuntimeError("historical collection is not complete; backtest preparation refuses partial data")
    db = _open_db(cache_dir)
    try:
        if force:
            db.execute("DELETE FROM local_features")
            db.execute("DELETE FROM prepared_symbols")
            db.execute("DELETE FROM reconstructed_signals")
            db.execute("DELETE FROM meta WHERE key LIKE 'reconstruct_%'")
            db.commit()
        symbols = sorted(p.name for p in (cache_dir / "candles" / "Min15").iterdir() if p.is_dir())
        done = {r[0] for r in db.execute("SELECT symbol FROM prepared_symbols")}
        total_rows = db.execute("SELECT COUNT(*) FROM local_features").fetchone()[0]
        for pos, symbol in enumerate(symbols, 1):
            if symbol in done:
                continue
            rows = _prepare_symbol_rows(cache_dir, symbol)
            with db:
                db.executemany(
                    """INSERT OR REPLACE INTO local_features VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    rows,
                )
                db.execute(
                    "INSERT OR REPLACE INTO prepared_symbols(symbol,rows,completed_at) VALUES(?,?,?)",
                    (symbol, len(rows), datetime.now(UTC).isoformat()),
                )
            total_rows += len(rows)
            if pos % 10 == 0 or rows:
                LOGGER.info("Backtest prepare %d/%d symbol=%s rows=%d total_rows=%d", pos, len(symbols), symbol, len(rows), total_rows)
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('prepare_schema',?)", (str(SCHEMA_VERSION),))
        db.commit()
        return {
            "symbols_total": len(symbols),
            "symbols_prepared": db.execute("SELECT COUNT(*) FROM prepared_symbols").fetchone()[0],
            "feature_rows": db.execute("SELECT COUNT(*) FROM local_features").fetchone()[0],
            "sqlite_path": str(cache_dir / "backtest" / "historical-backtest.sqlite"),
        }
    finally:
        db.close()


def _feature_ex(row: sqlite3.Row | tuple[Any, ...]) -> ExhaustionFeatures:
    return ExhaustionFeatures(
        upper_wick_ratio_15m=row[20], close_location_15m=row[21], momentum_1h=row[18], previous_momentum_1h=row[19],
        momentum_decelerating=(row[18] is not None and row[19] is not None and row[18] < row[19]),
        below_ema9_15m=bool(row[22]), lower_high_and_close=bool(row[23]), structural_break_15m=bool(row[24]), volume_zscore_15m=row[6],
    )


def _episodes_to_json(episodes: dict[str, Episode]) -> str:
    return json.dumps({
        k: {
            **asdict(v),
            "started_at": v.started_at.isoformat(), "peak_at": v.peak_at.isoformat(),
            "breakdown_at": v.breakdown_at.isoformat() if v.breakdown_at else None,
            "confirmed_short_at": v.confirmed_short_at.isoformat() if v.confirmed_short_at else None,
        } for k, v in episodes.items()
    }, separators=(",", ":"))


def _episodes_from_json(raw: str | None) -> dict[str, Episode]:
    if not raw:
        return {}
    parsed = json.loads(raw)
    out: dict[str, Episode] = {}
    for k, v in parsed.items():
        out[k] = Episode(
            symbol=v["symbol"], started_at=_parse_iso(v["started_at"]), state=v["state"], peak_price=float(v["peak_price"]), peak_at=_parse_iso(v["peak_at"]),
            run_score=int(v["run_score"]), exhaustion_score=int(v["exhaustion_score"]), broken_level=v.get("broken_level"),
            breakdown_at=_parse_iso(v["breakdown_at"]) if v.get("breakdown_at") else None, breakdown_atr_15m=v.get("breakdown_atr_15m"),
            confirmed_short_at=_parse_iso(v["confirmed_short_at"]) if v.get("confirmed_short_at") else None,
        )
    return out


@functools.lru_cache(maxsize=96)
def _day_candles(cache_dir_s: str, symbol: str) -> tuple[Candle, ...]:
    return tuple(_chunk_candles(Path(cache_dir_s), symbol, "Day1"))


@functools.lru_cache(maxsize=96)
def _min15_candles(cache_dir_s: str, symbol: str) -> tuple[Candle, ...]:
    return tuple(_chunk_candles(Path(cache_dir_s), symbol, "Min15"))


def _retest_from_cache(cache_dir: Path, symbol: str, breakdown_at: datetime, level: float, atr15: float, now: datetime):
    candles = [c for c in _min15_candles(str(cache_dir), symbol) if c.open_time + timedelta(minutes=15) <= now]
    return evaluate_failed_retest(candles[-400:], breakdown_at=breakdown_at, broken_level=level, atr_15m=atr15, tolerance_atr=RETEST_TOLERANCE_ATR, window_candles=RETEST_WINDOW_CANDLES)


def reconstruct_signals(cache_dir: Path, *, force: bool = False) -> tuple[list[ReconstructedSignal], ReconstructionStats]:
    prepare_features(cache_dir, force=False)
    ready = readiness(cache_dir)
    db = _open_db(cache_dir)
    db.row_factory = sqlite3.Row
    stats = ReconstructionStats(fidelity_notes=list(ready["fidelity"].values()))
    try:
        if force:
            db.execute("DELETE FROM reconstructed_signals")
            db.execute("DELETE FROM meta WHERE key LIKE 'reconstruct_%'")
            db.commit()
        stats.symbols_prepared = db.execute("SELECT COUNT(*) FROM prepared_symbols").fetchone()[0]
        stats.feature_rows = db.execute("SELECT COUNT(*) FROM local_features").fetchone()[0]
        cursor_raw = db.execute("SELECT value FROM meta WHERE key='reconstruct_next_ts'").fetchone()
        ep_raw = db.execute("SELECT value FROM meta WHERE key='reconstruct_episodes'").fetchone()
        episodes = _episodes_from_json(ep_raw[0] if ep_raw else None)
        start_ts = int(cursor_raw[0]) if cursor_raw else None
        all_times = [r[0] for r in db.execute("SELECT DISTINCT ts FROM local_features ORDER BY ts")]
        if start_ts is not None:
            all_times = [x for x in all_times if x >= start_ts]

        for step_i, ts_val in enumerate(all_times):
            when = _dt(ts_val)
            rows = list(db.execute("SELECT * FROM local_features WHERE ts=?", (ts_val,)))
            if not rows:
                continue
            stats.evaluation_points += 1
            returns = [float(r[3]) for r in rows]
            btc = next((r for r in rows if r[1] == "BTC_USDT"), None)
            btc_r24 = float(btc[3]) if btc is not None else None
            ranked: list[tuple[int, float, float, sqlite3.Row, float | None]] = []
            for r in rows:
                symbol = str(r[1])
                if symbol in EXCLUDED_SYMBOLS:
                    continue
                rank = percentile_rank(float(r[3]), returns)
                active = symbol in episodes
                standard = float(r[5]) >= MIN_AMOUNT_24H
                mover = float(r[3]) >= DISCOVERY_MIN_RETURN_24H
                relative = rank is not None and rank >= DISCOVERY_MIN_CROSS_SECTION_PERCENTILE
                mover72 = float(r[4]) >= WIDE_SCAN_MIN_RETURN_72H
                if not (active or standard or mover or relative or mover72):
                    continue
                ranked.append((2 if active else (1 if mover72 else 0), max(float(r[3]), float(r[4])), float(r[5]), r, rank))
            ranked.sort(key=lambda x: (x[0], x[1], x[2], str(x[3][1])), reverse=True)

            for _active_rank, _move, _amt, r, cross_rank in ranked[:MAX_SYMBOLS]:
                symbol = str(r[1])
                amount24 = float(r[5])
                if amount24 < HIGH_RISK_MIN_AMOUNT_24H:
                    stats.amount_proxy_suppressed += 1
                    continue
                risk_tier = "standard" if amount24 >= MIN_AMOUNT_24H else "high_risk"
                rf = RunFeatures(
                    return_24h=float(r[3]), return_72h=float(r[4]), btc_return_24h=btc_r24,
                    residual_return_24h=(float(r[3])-btc_r24) if btc_r24 is not None else None,
                    cross_section_percentile=cross_rank, volume_zscore_15m=r[6], distance_above_ema20_atr_4h=r[7],
                    amount_24h=amount24, spread_pct=None, funding_rate=None, fair_index_premium_pct=None, hold_vol=None,
                )
                run_score, _, scorable = score_run(rf, RUN_THRESHOLDS)
                ex = _feature_ex(r)
                ex_score = int(r[25])
                state, _ = classify_market_state(rf, run_score, ex, ex_score, STATE_THRESHOLDS)
                normal_valid = scorable and state is not None
                episode = episodes.get(symbol)

                if episode and when - episode.started_at > timedelta(hours=EPISODE_MAX_AGE_HOURS):
                    episodes.pop(symbol, None); episode = None
                if episode and episode.confirmed_short_at is None and float(r[10]) > episode.peak_price:
                    episode.peak_price = float(r[10]); episode.peak_at = _dt(int(r[11]))
                if episode and episode.confirmed_short_at is not None:
                    rearm_high = normal_valid and float(r[10]) >= episode.peak_price * (1+REARM_NEW_HIGH_PCT)
                    rearm_time = normal_valid and (when-episode.confirmed_short_at).total_seconds()/3600 >= CONFIRMED_REARM_HOURS
                    if rearm_high or rearm_time:
                        episodes.pop(symbol, None); episode = None
                    else:
                        continue

                if episode and episode.state == "breakdown_watch":
                    if episode.breakdown_at is None or episode.broken_level is None or episode.breakdown_atr_15m is None:
                        episode.state = state or "exhaustion_watch"; episode.breakdown_at = None; episode.broken_level = None; episode.breakdown_atr_15m = None
                    else:
                        retest = _retest_from_cache(cache_dir, symbol, episode.breakdown_at, episode.broken_level, episode.breakdown_atr_15m, when)
                        if retest.confirmed:
                            entry = float(retest.retest_close or r[2])
                            features = rf.as_dict(); features.update(ex.as_dict())
                            features.update({
                                "run_score": run_score, "exhaustion_score": ex_score, "atr_15m": r[8], "risk_tier": risk_tier,
                                "episode_peak_price": episode.peak_price, "episode_started_at": episode.started_at.isoformat(),
                                "broken_level": episode.broken_level, "breakdown_at": episode.breakdown_at.isoformat(),
                                "hours_run_to_breakdown": max(0.0, (episode.breakdown_at-episode.started_at).total_seconds()/3600),
                                "retest_at": retest.retest_at.isoformat() if retest.retest_at else None,
                                "retest_high": retest.retest_high, "retest_close": retest.retest_close,
                            })
                            day_rows = [{"open_time": c.open_time, "high": c.high, "low": c.low, "close": c.close} for c in _day_candles(str(cache_dir), symbol)]
                            daily, _ = reconstruct_daily_regime_features(confirmed_at=when, entry_price=entry, day1_rows=day_rows)
                            features.update(daily)
                            core = daily_confirmed_core_v1_state(features)
                            if core is None:
                                stats.missing_daily_core += 1
                            elif core:
                                stats.daily_core_skips += 1
                            else:
                                pers = daily_bull_persistence_v2_state(features)
                                if pers is None:
                                    stats.missing_persistence += 1
                                elif pers:
                                    stats.persistence_skips += 1
                                else:
                                    with db:
                                        db.execute("INSERT OR IGNORE INTO reconstructed_signals VALUES(?,?,?,?,?)", (symbol, ts_val, entry, risk_tier, json.dumps(features, separators=(",", ":"), default=str)))
                                    stats.admitted_after_filters += 1
                            stats.confirmed_signals += 1
                            episode.confirmed_short_at = when; episode.state = "confirmed_short"
                            continue
                        if retest.invalidated or retest.expired:
                            episode.state = state or "exhaustion_watch"; episode.breakdown_at=None; episode.broken_level=None; episode.breakdown_atr_15m=None
                            if retest.invalidated:
                                continue
                        else:
                            continue

                if episode and not normal_valid:
                    anchor = max(episode.started_at, episode.peak_at)
                    if (when-anchor).total_seconds()/3600 > ARMED_RUNNER_MEMORY_HOURS:
                        episodes.pop(symbol, None); continue
                    if episode.state == "exhaustion_watch" or armed_runner_exhaustion_ready(ex, ex_score, STATE_THRESHOLDS):
                        state = "exhaustion_watch"; episode.state = "exhaustion_watch"
                    else:
                        continue
                if episode is None and not normal_valid:
                    continue
                if episode is None:
                    episode = Episode(symbol, when, str(state), float(r[10]), _dt(int(r[11])), run_score, ex_score); episodes[symbol]=episode
                else:
                    episode.state=str(state); episode.run_score=run_score; episode.exhaustion_score=ex_score
                    if float(r[10]) > episode.peak_price:
                        episode.peak_price=float(r[10]); episode.peak_at=_dt(int(r[11]))
                if state == "exhaustion_watch" and bool(r[24]) and ex_score >= SHORT_EXHAUSTION_SCORE and r[8] is not None and float(r[8]) > 0:
                    episode.state="breakdown_watch"; episode.broken_level=float(r[9]); episode.breakdown_at=when-timedelta(minutes=15); episode.breakdown_atr_15m=float(r[8])

            if step_i % 96 == 0:
                next_ts = all_times[step_i+1] if step_i+1 < len(all_times) else ts_val+900
                with db:
                    db.execute("INSERT OR REPLACE INTO meta VALUES('reconstruct_next_ts',?)", (str(next_ts),))
                    db.execute("INSERT OR REPLACE INTO meta VALUES('reconstruct_episodes',?)", (_episodes_to_json(episodes),))
                LOGGER.info("Signal reconstruction progress %d/%d ts=%s signals=%d", step_i+1, len(all_times), when.isoformat(), db.execute("SELECT COUNT(*) FROM reconstructed_signals").fetchone()[0])

        with db:
            db.execute("DELETE FROM meta WHERE key='reconstruct_next_ts'")
            db.execute("DELETE FROM meta WHERE key='reconstruct_episodes'")
            db.execute("INSERT OR REPLACE INTO meta VALUES('reconstruct_complete','1')")
        stats.admitted_after_filters = db.execute("SELECT COUNT(*) FROM reconstructed_signals").fetchone()[0]
        signals = [ReconstructedSignal(str(r[0]), _dt(r[1]), float(r[2]), str(r[3]), json.loads(r[4])) for r in db.execute("SELECT symbol,confirmed_ts,entry_price,risk_tier,features_json FROM reconstructed_signals ORDER BY confirmed_ts,symbol")]
        _atomic_json(cache_dir / "backtest" / "reconstruction-summary.json", {"schema": SCHEMA_VERSION, "created_at": datetime.now(UTC).isoformat(), "stats": asdict(stats)})
        return signals, stats
    finally:
        db.close()


def entry_quality(features: dict[str, Any]) -> int:
    def f(name: str) -> float | None:
        try:
            v=features.get(name); return float(v) if v is not None else None
        except (TypeError, ValueError): return None
    exhaustion, run_score=f("exhaustion_score"),f("run_score"); amount,volume_z=f("amount_24h"),f("volume_zscore_15m")
    premium,r24,r72=f("fair_index_premium_pct"),f("return_24h"),f("return_72h"); momentum=f("momentum_1h")
    q=0
    q+=2 if exhaustion is not None and 4<exhaustion<=5 else 0; q+=2 if run_score is not None and 3.667<run_score<=5 else 0
    q+=1 if amount is not None and amount>12_310_000 else 0; q+=1 if volume_z is not None and volume_z<=-0.2796 else 0
    q+=1 if premium is not None and premium<=-0.04249 else 0; q+=1 if r24 is not None and r24>0.2482 else 0
    q+=1 if r72 is not None and r72>0.5629 else 0; q+=1 if momentum is not None and momentum<=-0.04472 else 0
    return min(10,q)


def _position_event(cache_dir: Path, sig: ReconstructedSignal, quality: int, end: datetime) -> tuple[datetime|None,float|None,str|None]:
    candles = _min15_candles(str(cache_dir), sig.symbol)
    times=[c.open_time for c in candles]
    i=bisect.bisect_left(times, sig.confirmed_at)
    tp=sig.entry_price*0.95; sl=sig.entry_price*2.0; lae=sig.entry_price*1.10
    for c in candles[i:]:
        t=c.open_time+timedelta(minutes=15)
        if t>end: break
        if c.high>=sl: return t,sl,"sl100"
        if quality<=1 and t>=sig.confirmed_at+timedelta(hours=24) and c.high>=lae: return t,lae,"lae10_24_q1"
        if c.low<=tp: return t,tp,"tp5"
    return None,None,None


def _mark(cache_dir: Path,symbol:str,when:datetime)->float|None:
    candles=_min15_candles(str(cache_dir),symbol); times=[c.open_time for c in candles]
    i=bisect.bisect_right(times,when-timedelta(minutes=15))-1
    return candles[i].close if i>=0 else None


def replay(cache_dir: Path, signals:list[ReconstructedSignal], *, exposure_pct:float, slots:int=6)->ReplayResult:
    start,end=_load_window(cache_dir); fraction=(exposure_pct/100)/slots
    positions:list[Position]=[]; entered=closed_wins=closed_losses=capacity=same=0; equity=1.0; peak_eq=1.0; max_dd=0.0
    peak_exposure=0.0; exposure_quarters=0.0; worst_open=None
    by_ts:dict[int,list[ReconstructedSignal]]=collections.defaultdict(list)
    for s in signals: by_ts[_ts(s.confirmed_at)].append(s)
    exit_cache:dict[tuple[str,int],tuple[datetime|None,float|None,str|None]]={}

    def close_due(t:datetime)->None:
        nonlocal equity,closed_wins,closed_losses
        due=sorted([p for p in positions if p.exit_at and p.exit_at<=t],key=lambda p:p.exit_at or t)
        for p in due:
            ret=(p.entry_price-float(p.exit_price))/p.entry_price
            equity += p.notional*(ret-FEE_PER_FILL); positions.remove(p)
            if ret>0: closed_wins+=1
            else: closed_losses+=1

    t=start.replace(second=0,microsecond=0)
    minute=t.minute%15
    if minute: t += timedelta(minutes=15-minute)
    while t<=end:
        close_due(t)
        for sig in sorted(by_ts.get(_ts(t),[]),key=lambda x:x.symbol):
            if any(p.symbol==sig.symbol for p in positions): same+=1; continue
            if len(positions)>=slots: capacity+=1; continue
            q=entry_quality(sig.features); key=(sig.symbol,_ts(sig.confirmed_at))
            if key not in exit_cache: exit_cache[key]=_position_event(cache_dir,sig,q,end)
            xa,xp,xr=exit_cache[key]; notional=max(0.0,equity)*fraction; equity-=notional*FEE_PER_FILL
            positions.append(Position(sig.symbol,sig.confirmed_at,sig.entry_price,notional,fraction,q,xa,xp,xr)); entered+=1
            peak_exposure=max(peak_exposure,sum(p.fraction for p in positions))
        marked=equity
        for p in positions:
            price=_mark(cache_dir,p.symbol,t)
            if price is not None: marked += p.notional*((p.entry_price-price)/p.entry_price)
        peak_eq=max(peak_eq,marked)
        if peak_eq>0: max_dd=min(max_dd,marked/peak_eq-1)
        exposure_quarters += sum(p.fraction for p in positions)
        t += timedelta(minutes=15)
    close_due(end)
    marked=equity
    for p in positions:
        price=_mark(cache_dir,p.symbol,end)
        if price is None: continue
        ret=(p.entry_price-price)/p.entry_price; worst_open=ret if worst_open is None else min(worst_open,ret)
        marked += p.notional*(ret-FEE_PER_FILL)
    total=marked-1; days=max(1e-9,(end-start).total_seconds()/86400)
    geo=(marked**(30/days)-1) if marked>0 else None
    periods=max(1,math.ceil((end-start).total_seconds()/900))
    return ReplayResult(exposure_pct,slots,total*100,geo*100 if geo is not None else None,total*30/days*100,max_dd*100,entered,closed_wins,closed_losses,len(positions),capacity,same,len(signals),exposure_quarters/periods*100,peak_exposure*100,worst_open*100 if worst_open is not None else None,marked)


def run_backtest(cache_dir:Path, exposures:list[float], slots:int, force_reconstruct:bool)->dict[str,Any]:
    ready=readiness(cache_dir)
    if not ready["ready"]: raise RuntimeError("historical data collection is not complete yet")
    prep=prepare_features(cache_dir); signals,stats=reconstruct_signals(cache_dir,force=force_reconstruct)
    results=[replay(cache_dir,signals,exposure_pct=e,slots=slots) for e in exposures]
    report={"schema":SCHEMA_VERSION,"created_at":datetime.now(UTC).isoformat(),"strategy":"TP5 / SL100 + LAE10/24-Q1 / Daily-Core + Persistence V2","slots":slots,"exposures_pct":exposures,"preparation":prep,"reconstruction":asdict(stats),"fidelity":ready["fidelity"],"results":[asdict(r) for r in results]}
    _atomic_json(cache_dir/"backtest"/"six-month-backtest.json",report); return report


def build_parser()->argparse.ArgumentParser:
    p=argparse.ArgumentParser(description="Offline DB-isolated six-month as-if-live reconstruction/backtest")
    sub=p.add_subparsers(dest="command",required=True)
    rd=sub.add_parser("readiness"); rd.add_argument("--cache-dir",default="research-history-v2")
    pr=sub.add_parser("prepare"); pr.add_argument("--cache-dir",default="research-history-v2"); pr.add_argument("--force",action="store_true")
    rc=sub.add_parser("reconstruct"); rc.add_argument("--cache-dir",default="research-history-v2"); rc.add_argument("--force",action="store_true")
    run=sub.add_parser("run"); run.add_argument("--cache-dir",default="research-history-v2"); run.add_argument("--exposures",default="50,75,100"); run.add_argument("--slots",type=int,default=6); run.add_argument("--force-reconstruct",action="store_true")
    return p


def main()->int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    a=build_parser().parse_args(); cache=Path(a.cache_dir).expanduser().resolve()
    if a.command=="readiness": print(json.dumps(readiness(cache),indent=2,sort_keys=True,default=str)); return 0
    if a.command=="prepare": print(json.dumps(prepare_features(cache,force=a.force),indent=2,sort_keys=True)); return 0
    if a.command=="reconstruct":
        sig,st=reconstruct_signals(cache,force=a.force); print(json.dumps({"signals":len(sig),"stats":asdict(st)},indent=2,sort_keys=True,default=str)); return 0
    ex=[float(x.strip()) for x in a.exposures.split(",") if x.strip()]
    if not ex or any(x<=0 or x>100 for x in ex): raise ValueError("exposures must be in (0,100]")
    if a.slots<=0: raise ValueError("slots must be positive")
    print(json.dumps(run_backtest(cache,ex,a.slots,a.force_reconstruct),indent=2,sort_keys=True,default=str)); return 0


if __name__=="__main__": raise SystemExit(main())

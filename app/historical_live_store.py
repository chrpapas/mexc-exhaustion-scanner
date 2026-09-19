from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import logging
import math
import os
import sqlite3
import tempfile
from bisect import bisect_right
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.historical_research import GentleLimiter
from app.mexc import INTERVAL_SECONDS, parse_klines
from app.models import Candle, Ticker

try:
    import httpx
except ImportError:  # pragma: no cover - project dependency
    httpx = None  # type: ignore[assignment]

LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 1
DEFAULT_FUTURES_BASE_URL = "https://contract.mexc.com"


@dataclass(frozen=True, slots=True)
class SpreadProxyModel:
    """Deterministic historical spread proxy calibrated from live scanner snapshots.

    MEXC exposes historical trade/index/fair-price candles and funding history, but
    not historical bid/ask snapshots. The scanner only uses spread for execution
    tier classification. We therefore calibrate one representative spread per
    24h-turnover band and validate the *risk-tier classification* rather than
    pretending historical order-book spread is exact.
    """

    high_risk_min_amount_24h: float = 500_000.0
    standard_min_amount_24h: float = 3_000_000.0
    standard_max_spread_pct: float = 0.35
    high_risk_max_spread_pct: float = 1.0
    low_amount_spread_pct: float = 1.50
    high_risk_spread_pct: float = 0.05
    standard_spread_pct: float = 0.02
    source: str = "default_amount_band_proxy"

    def spread_pct(self, amount24: float) -> float:
        if amount24 >= self.standard_min_amount_24h:
            return self.standard_spread_pct
        if amount24 >= self.high_risk_min_amount_24h:
            return self.high_risk_spread_pct
        return self.low_amount_spread_pct

    def tier(self, amount24: float, spread_pct: float | None = None) -> str:
        spread = self.spread_pct(amount24) if spread_pct is None else spread_pct
        if amount24 >= self.standard_min_amount_24h and spread <= self.standard_max_spread_pct:
            return "standard"
        if amount24 >= self.high_risk_min_amount_24h and spread <= self.high_risk_max_spread_pct:
            return "high_risk"
        return "extreme_risk"


@dataclass(frozen=True, slots=True)
class SpreadCalibrationResult:
    rows: int
    symbols: int
    started_at: str | None
    ended_at: str | None
    accuracy: float
    standard_recall: float
    high_risk_recall: float
    extreme_recall: float
    model: SpreadProxyModel


@dataclass(frozen=True, slots=True)
class SnapshotProvenance:
    contract_ohlcv: str = "mexc_contract_min5"
    index_price: str = "mexc_index_price_min5"
    fair_price: str = "mexc_fair_price_min5"
    funding_rate: str = "mexc_funding_settlement_carry_forward"
    spread: str = "calibrated_amount_band_proxy"
    hold_vol: str = "unavailable"
    cadence: str = "5m"


def _quantile(values: Sequence[float], q: float, fallback: float) -> float:
    if not values:
        return fallback
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    frac = pos - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def calibrate_spread_proxy(live_ticker_db: Path) -> SpreadCalibrationResult:
    """Fit the amount-band proxy and report execution-tier agreement.

    Calibration uses the exact scanner ticker schema. To avoid overfitting to
    outliers, representative spreads are medians within the amount bands; tier
    quality is measured against the real spread-based production classification.
    """

    db = sqlite3.connect(live_ticker_db)
    try:
        rows = db.execute(
            """
            SELECT symbol, observed_at, amount24, spread_pct
            FROM ticker_snapshots
            WHERE amount24 IS NOT NULL AND spread_pct IS NOT NULL
            """
        )
        low: list[float] = []
        high: list[float] = []
        standard: list[float] = []
        records: list[tuple[str, str, float, float]] = []
        symbols: set[str] = set()
        start: str | None = None
        end: str | None = None
        for symbol, observed_at, amount24, spread_pct in rows:
            amount = float(amount24)
            spread = float(spread_pct)
            records.append((str(symbol), str(observed_at), amount, spread))
            symbols.add(str(symbol))
            start = str(observed_at) if start is None or str(observed_at) < start else start
            end = str(observed_at) if end is None or str(observed_at) > end else end
            if amount >= 3_000_000.0:
                standard.append(spread)
            elif amount >= 500_000.0:
                high.append(spread)
            else:
                low.append(spread)
    finally:
        db.close()

    model = SpreadProxyModel(
        low_amount_spread_pct=max(1.01, _quantile(low, 0.50, 1.50)),
        high_risk_spread_pct=min(0.99, _quantile(high, 0.50, 0.05)),
        standard_spread_pct=min(0.34, _quantile(standard, 0.50, 0.02)),
        source=f"median_amount_bands:{live_ticker_db.name}",
    )

    if not records:
        return SpreadCalibrationResult(0, 0, start, end, 0.0, 0.0, 0.0, 0.0, model)

    totals = {"standard": 0, "high_risk": 0, "extreme_risk": 0}
    hits = {"standard": 0, "high_risk": 0, "extreme_risk": 0}
    correct = 0
    for _symbol, _at, amount, actual_spread in records:
        actual = model.tier(amount, actual_spread)
        predicted = model.tier(amount)
        totals[actual] += 1
        if predicted == actual:
            correct += 1
            hits[actual] += 1

    def recall(name: str) -> float:
        return hits[name] / totals[name] if totals[name] else 1.0

    return SpreadCalibrationResult(
        rows=len(records),
        symbols=len(symbols),
        started_at=start,
        ended_at=end,
        accuracy=correct / len(records),
        standard_recall=recall("standard"),
        high_risk_recall=recall("high_risk"),
        extreme_recall=recall("extreme_risk"),
        model=model,
    )


def _mid_bid_ask(last: float, spread_pct: float) -> tuple[float, float]:
    half = max(0.0, spread_pct) / 200.0
    return last * (1.0 - half), last * (1.0 + half)


def _latest_close_at_or_before(candles: Sequence[Candle], observed_at: datetime) -> float | None:
    times = [c.open_time for c in candles]
    idx = bisect_right(times, observed_at) - 1
    if idx < 0:
        return None
    return candles[idx].close


def _funding_at(history: Sequence[tuple[datetime, float]], observed_at: datetime) -> float | None:
    if not history:
        return None
    times = [row[0] for row in history]
    idx = bisect_right(times, observed_at) - 1
    if idx < 0:
        return history[0][1]
    return history[idx][1]


def derive_ticker_snapshots(
    symbol: str,
    contract_5m: Sequence[Candle],
    *,
    index_5m: Sequence[Candle] = (),
    fair_5m: Sequence[Candle] = (),
    funding_history: Sequence[tuple[datetime, float]] = (),
    spread_model: SpreadProxyModel | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Iterable[Ticker]:
    """Emit 5m historical snapshots in the exact live ``Ticker`` shape.

    Candle ``open_time`` is the start of the MEXC 5m bar. A historical scanner
    tick at ``open_time + 5m`` may safely see that completed bar. The rolling 24h
    fields use the most recent 288 completed 5m bars, matching a causal window.
    """

    model = spread_model or SpreadProxyModel()
    window: deque[Candle] = deque()
    amount_sum = 0.0
    volume_sum = 0.0
    for candle in contract_5m:
        observed_at = candle.open_time + timedelta(minutes=5)
        if start is not None and observed_at < start:
            # Still populate the rolling window so the first requested snapshot
            # has causal 24h history.
            pass
        while window and window[0].open_time < candle.open_time - timedelta(hours=24) + timedelta(minutes=5):
            old = window.popleft()
            amount_sum -= old.amount
            volume_sum -= old.volume
        window.append(candle)
        amount_sum += candle.amount
        volume_sum += candle.volume

        if start is not None and observed_at < start:
            continue
        if end is not None and observed_at > end:
            break
        if len(window) < 2:
            continue

        first = window[0]
        baseline = first.open if first.open > 0 else first.close
        rise = candle.close / baseline - 1.0 if baseline > 0 else 0.0
        low24 = min(item.low for item in window)
        high24 = max(item.high for item in window)
        spread_pct = model.spread_pct(amount_sum)
        bid, ask = _mid_bid_ask(candle.close, spread_pct)
        yield Ticker(
            symbol=symbol,
            observed_at=observed_at,
            last_price=candle.close,
            bid1=bid,
            ask1=ask,
            amount24=max(0.0, amount_sum),
            volume24=max(0.0, volume_sum),
            hold_vol=None,
            low24=low24,
            high24=high24,
            rise_fall_rate=rise,
            index_price=_latest_close_at_or_before(index_5m, candle.open_time),
            fair_price=_latest_close_at_or_before(fair_5m, candle.open_time),
            funding_rate=_funding_at(funding_history, observed_at),
        )


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(microsecond=0)


def _atomic_gzip_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    Path(name).unlink(missing_ok=True)
    try:
        with gzip.open(name, "wt", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def _read_kline_cache(root: Path, symbol: str) -> list[Candle]:
    rows: dict[datetime, Candle] = {}
    if not root.exists():
        return []
    for path in sorted((root / symbol).glob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        for candle in parse_klines(symbol, "Min5", payload.get("data", payload)):
            rows[candle.open_time] = candle
    return [rows[key] for key in sorted(rows)]


def _read_funding_cache(path: Path) -> list[tuple[datetime, float]]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    result: list[tuple[datetime, float]] = []
    for row in raw:
        try:
            result.append((datetime.fromtimestamp(int(row["settleTime"]) / 1000, UTC), float(row["fundingRate"])))
        except (KeyError, TypeError, ValueError, OSError):
            continue
    return sorted(result)


def _init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS ticker_snapshots (
            symbol TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            last_price REAL,
            bid1 REAL,
            ask1 REAL,
            spread_pct REAL,
            amount24 REAL,
            volume24 REAL,
            hold_vol REAL,
            low24 REAL,
            high24 REAL,
            rise_fall_rate REAL,
            index_price REAL,
            fair_price REAL,
            funding_rate REAL,
            PRIMARY KEY (symbol, observed_at)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS provenance (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        ) WITHOUT ROWID;
        """
    )
    return db


def build_sqlite_from_cache(
    cache_dir: Path,
    output_db: Path,
    *,
    start: datetime,
    end: datetime,
    spread_model: SpreadProxyModel,
) -> dict[str, Any]:
    universe_file = cache_dir / "universe-history" / "historical-all-symbols.txt"
    if not universe_file.exists():
        raise RuntimeError("historical universe missing; run app.historical_universe first")
    symbols = [line.strip().upper() for line in universe_file.read_text().splitlines() if line.strip()]
    db = _init_db(output_db)
    inserted = 0
    symbol_count = 0
    try:
        for symbol in symbols:
            contract = _read_kline_cache(cache_dir / "live-store" / "contract" / "Min5", symbol)
            if not contract:
                continue
            index = _read_kline_cache(cache_dir / "live-store" / "index" / "Min5", symbol)
            fair = _read_kline_cache(cache_dir / "live-store" / "fair" / "Min5", symbol)
            funding = _read_funding_cache(cache_dir / "live-store" / "funding" / f"{symbol}.json")
            rows = []
            for ticker in derive_ticker_snapshots(
                symbol,
                contract,
                index_5m=index,
                fair_5m=fair,
                funding_history=funding,
                spread_model=spread_model,
                start=start,
                end=end,
            ):
                rows.append(
                    (
                        ticker.symbol,
                        ticker.observed_at.isoformat(sep=" "),
                        ticker.last_price,
                        ticker.bid1,
                        ticker.ask1,
                        ticker.spread_pct,
                        ticker.amount24,
                        ticker.volume24,
                        ticker.hold_vol,
                        ticker.low24,
                        ticker.high24,
                        ticker.rise_fall_rate,
                        ticker.index_price,
                        ticker.fair_price,
                        ticker.funding_rate,
                    )
                )
            if rows:
                db.executemany(
                    """
                    INSERT OR REPLACE INTO ticker_snapshots(
                        symbol, observed_at, last_price, bid1, ask1, spread_pct,
                        amount24, volume24, hold_vol, low24, high24, rise_fall_rate,
                        index_price, fair_price, funding_rate
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    rows,
                )
                db.commit()
                inserted += len(rows)
                symbol_count += 1
        provenance = SnapshotProvenance()
        db.execute(
            "INSERT OR REPLACE INTO provenance(key,value_json) VALUES (?,?)",
            ("snapshot_provenance", json.dumps(asdict(provenance), sort_keys=True)),
        )
        db.execute(
            "INSERT OR REPLACE INTO provenance(key,value_json) VALUES (?,?)",
            ("spread_model", json.dumps(asdict(spread_model), sort_keys=True)),
        )
        db.commit()
    finally:
        db.close()
    return {"symbols": symbol_count, "snapshots": inserted, "output": str(output_db)}


class HistoricalLiveClient:
    def __init__(self, *, base_url: str, requests_per_second: float, timeout_seconds: float = 25.0) -> None:
        if httpx is None:  # pragma: no cover
            raise RuntimeError("httpx is required")
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=httpx.Timeout(timeout_seconds))
        self.limiter = GentleLimiter(requests_per_second)

    async def close(self) -> None:
        await self.client.aclose()

    async def _json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        last: Exception | None = None
        for attempt in range(1, 6):
            try:
                await self.limiter.wait()
                response = await self.client.get(path, params=params)
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict) and payload.get("success") is False:
                    raise RuntimeError(f"MEXC error {payload.get('code')}: {payload.get('message') or payload.get('msg')}")
                return payload.get("data", payload) if isinstance(payload, dict) else payload
            except Exception as exc:  # network boundary; retries are intentional
                last = exc
                if attempt < 5:
                    await asyncio.sleep(min(2 ** (attempt - 1), 20))
        raise RuntimeError(f"GET {path} failed after retries: {last}")

    async def kline(self, symbol: str, kind: str, start: datetime, end: datetime) -> dict[str, Any]:
        prefix = {"contract": "", "index": "index_price/", "fair": "fair_price/"}[kind]
        data = await self._json(
            f"/api/v1/contract/kline/{prefix}{symbol}",
            {"interval": "Min5", "start": int(start.timestamp()), "end": int(end.timestamp())},
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"unexpected {kind} kline payload for {symbol}")
        return data

    async def funding_history(self, symbol: str, start: datetime) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await self._json(
                "/api/v1/contract/funding_rate/history",
                {"symbol": symbol, "page_num": page, "page_size": 1000},
            )
            if not isinstance(data, dict):
                break
            rows = [row for row in data.get("resultList", []) if isinstance(row, dict)]
            result.extend(rows)
            if not rows:
                break
            oldest_ms = min(int(row.get("settleTime") or 0) for row in rows)
            total_pages = int(data.get("totalPage") or page)
            if oldest_ms and datetime.fromtimestamp(oldest_ms / 1000, UTC) <= start:
                break
            if page >= total_pages:
                break
            page += 1
        return result


async def fetch_live_store(
    cache_dir: Path,
    *,
    start: datetime,
    end: datetime,
    requests_per_second: float = 2.0,
    base_url: str = DEFAULT_FUTURES_BASE_URL,
) -> dict[str, Any]:
    universe_file = cache_dir / "universe-history" / "historical-all-symbols.txt"
    if not universe_file.exists():
        raise RuntimeError("historical universe missing; run app.historical_universe first")
    symbols = [line.strip().upper() for line in universe_file.read_text().splitlines() if line.strip()]
    client = HistoricalLiveClient(base_url=base_url, requests_per_second=requests_per_second)
    chunks = 0
    failures: list[str] = []
    try:
        # 1900 is below MEXC's practical kline window. Keep one day overlap to
        # make resumptions idempotent and avoid edge gaps.
        span = timedelta(minutes=5 * 1800)
        warmup_start = start - timedelta(hours=24)
        for n, symbol in enumerate(symbols, start=1):
            LOGGER.info("historical-live fetch %d/%d %s", n, len(symbols), symbol)
            try:
                for kind in ("contract", "index", "fair"):
                    cursor = warmup_start
                    while cursor < end:
                        chunk_end = min(end, cursor + span)
                        target = (
                            cache_dir
                            / "live-store"
                            / kind
                            / "Min5"
                            / symbol
                            / f"{int(cursor.timestamp())}-{int(chunk_end.timestamp())}.json.gz"
                        )
                        if not target.exists():
                            payload = await client.kline(symbol, kind, cursor, chunk_end)
                            _atomic_gzip_json(target, {"data": payload})
                        chunks += 1
                        cursor = chunk_end + timedelta(minutes=5)
                funding_path = cache_dir / "live-store" / "funding" / f"{symbol}.json"
                if not funding_path.exists():
                    funding_path.parent.mkdir(parents=True, exist_ok=True)
                    funding = await client.funding_history(symbol, warmup_start)
                    funding_path.write_text(json.dumps(funding, separators=(",", ":"), sort_keys=True), encoding="utf-8")
            except Exception as exc:
                failures.append(f"{symbol}: {exc}")
                LOGGER.exception("historical-live fetch failed for %s", symbol)
    finally:
        await client.close()
    manifest = {
        "schema": SCHEMA_VERSION,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "symbols": len(symbols),
        "chunks_seen": chunks,
        "failures": failures,
        "sources": asdict(SnapshotProvenance()),
    }
    out = cache_dir / "live-store" / "manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build a six-month MEXC historical store in the live scanner ticker schema")
    sub = p.add_subparsers(dest="command", required=True)

    cal = sub.add_parser("calibrate-spread", help="calibrate/validate historical spread proxy from live ticker.sqlite")
    cal.add_argument("--ticker-db", required=True, type=Path)
    cal.add_argument("--output", type=Path)

    fetch = sub.add_parser("fetch", help="fetch 5m contract/index/fair history and funding into a resumable cache")
    fetch.add_argument("--cache-dir", required=True, type=Path)
    fetch.add_argument("--start", required=True)
    fetch.add_argument("--end", required=True)
    fetch.add_argument("--requests-per-second", type=float, default=2.0)

    build = sub.add_parser("build", help="materialize historical ticker_snapshots SQLite from fetched cache")
    build.add_argument("--cache-dir", required=True, type=Path)
    build.add_argument("--output-db", required=True, type=Path)
    build.add_argument("--start", required=True)
    build.add_argument("--end", required=True)
    build.add_argument("--spread-model-json", type=Path)
    return p


def _model_from_json(path: Path | None) -> SpreadProxyModel:
    if path is None:
        return SpreadProxyModel()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if "model" in raw:
        raw = raw["model"]
    allowed = set(SpreadProxyModel.__dataclass_fields__)
    return SpreadProxyModel(**{k: v for k, v in raw.items() if k in allowed})


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    args = build_parser().parse_args()
    if args.command == "calibrate-spread":
        result = calibrate_spread_proxy(args.ticker_db)
        payload = asdict(result)
        text = json.dumps(payload, indent=2, sort_keys=True)
        if args.output:
            args.output.write_text(text, encoding="utf-8")
        print(text)
        return
    if args.command == "fetch":
        result = asyncio.run(
            fetch_live_store(
                args.cache_dir,
                start=_parse_iso(args.start),
                end=_parse_iso(args.end),
                requests_per_second=args.requests_per_second,
            )
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if args.command == "build":
        result = build_sqlite_from_cache(
            args.cache_dir,
            args.output_db,
            start=_parse_iso(args.start),
            end=_parse_iso(args.end),
            spread_model=_model_from_json(args.spread_model_json),
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    raise SystemExit(2)


if __name__ == "__main__":
    main()

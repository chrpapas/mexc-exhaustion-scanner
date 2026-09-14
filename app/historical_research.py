from __future__ import annotations

import argparse
import asyncio
import fcntl
import gzip
import json
import logging
import math
import os
import random
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import httpx

from app.mexc import INTERVAL_SECONDS, is_crypto_usdt_contract, parse_spot_usdt_assets

LOGGER = logging.getLogger(__name__)

# This module is intentionally independent of app.db / trader_db / trader.  It is
# a public-market-data research collector only.  Never add production DB writes here.
DEFAULT_FUTURES_BASE_URL = "https://api.mexc.com"
DEFAULT_SPOT_BASE_URL = "https://api.mexc.com"
DEFAULT_INTERVALS = ("Min15", "Min60", "Hour4", "Day1")


@dataclass(frozen=True, slots=True)
class HistoricalFetchConfig:
    cache_dir: Path
    start: datetime
    end: datetime
    intervals: tuple[str, ...] = DEFAULT_INTERVALS
    requests_per_second: float = 1.0
    request_timeout_seconds: float = 20.0
    max_runtime_minutes: float = 15.0
    chunk_candles: int = 1200
    max_retries: int = 7
    max_cache_gb: float = 10.0
    max_symbols: int | None = None
    futures_base_url: str = DEFAULT_FUTURES_BASE_URL
    spot_base_url: str = DEFAULT_SPOT_BASE_URL
    seed_symbols_file: Path | None = None

    def validate(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start/end must be timezone-aware")
        if self.start >= self.end:
            raise ValueError("start must be before end")
        if not 0 < self.requests_per_second <= 2.0:
            raise ValueError("historical requests_per_second must be >0 and <=2.0")
        if not 1 <= self.chunk_candles <= 1500:
            raise ValueError("chunk_candles must be between 1 and 1500")
        if self.max_runtime_minutes <= 0:
            raise ValueError("max_runtime_minutes must be positive")
        if self.max_cache_gb <= 0:
            raise ValueError("max_cache_gb must be positive")
        for interval in self.intervals:
            if interval not in INTERVAL_SECONDS:
                raise ValueError(f"unsupported interval {interval}")


@dataclass(slots=True)
class RunStats:
    started_at: str
    stopped_at: str | None = None
    universe_symbols: int = 0
    planned_chunks: int = 0
    completed_chunks: int = 0
    skipped_cached_chunks: int = 0
    fetched_candles: int = 0
    failed_chunks: int = 0
    throttle_events: int = 0
    stop_reason: str | None = None


class GentleLimiter:
    """Single-process monotonic limiter with small jitter to avoid request bursts."""

    def __init__(self, requests_per_second: float) -> None:
        self._interval = 1.0 / requests_per_second
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            wait_for = self._interval - (now - self._last)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            # 0-150 ms jitter reduces accidental cadence synchronization.
            await asyncio.sleep(random.uniform(0.0, 0.15))
            self._last = loop.time()


class HistoricalPublicClient:
    """Very conservative, public-only MEXC reader for offline research.

    No API key is accepted.  429/403 responses cause long cool-downs and eventually
    a graceful abort instead of aggressive retries.
    """

    def __init__(self, config: HistoricalFetchConfig, stats: RunStats) -> None:
        self.config = config
        self.stats = stats
        timeout = httpx.Timeout(config.request_timeout_seconds)
        headers = {"User-Agent": "mexc-exhaustion-historical-research/1.3.69"}
        self._futures = httpx.AsyncClient(
            base_url=config.futures_base_url.rstrip("/"), timeout=timeout, headers=headers
        )
        self._spot = httpx.AsyncClient(
            base_url=config.spot_base_url.rstrip("/"), timeout=timeout, headers=headers
        )
        self._limiter = GentleLimiter(config.requests_per_second)

    async def close(self) -> None:
        await asyncio.gather(self._futures.aclose(), self._spot.aclose())

    async def _json(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        last: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                await self._limiter.wait()
                response = await client.get(path, params=params)
                if response.status_code in {403, 418, 429}:
                    self.stats.throttle_events += 1
                    retry_after = response.headers.get("Retry-After")
                    try:
                        server_wait = float(retry_after) if retry_after else 0.0
                    except ValueError:
                        server_wait = 0.0
                    # Back off much more aggressively than normal transient retries.
                    cooldown = max(server_wait, min(30.0 * (2 ** (attempt - 1)), 300.0))
                    LOGGER.warning(
                        "MEXC throttling/protection status=%s; cooling down %.1fs (attempt %d/%d)",
                        response.status_code,
                        cooldown,
                        attempt,
                        self.config.max_retries,
                    )
                    await asyncio.sleep(cooldown)
                    if self.stats.throttle_events >= 3:
                        raise RuntimeError("MEXC throttling guard tripped after 3 protection responses")
                    continue
                if 500 <= response.status_code < 600:
                    raise httpx.HTTPStatusError(
                        f"server error {response.status_code}", request=response.request, response=response
                    )
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict) and payload.get("success") is False:
                    raise RuntimeError(
                        f"MEXC error {payload.get('code')}: {payload.get('message') or payload.get('msg')}"
                    )
                return payload
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError, ValueError, RuntimeError) as exc:
                last = exc
                if "throttling guard tripped" in str(exc):
                    raise
                if attempt >= self.config.max_retries:
                    break
                await asyncio.sleep(min(2.0 ** (attempt - 1), 30.0) + random.uniform(0.0, 0.5))
        raise RuntimeError(f"GET {path} failed after retries: {last}")

    async def current_contracts(self) -> list[dict[str, Any]]:
        payload = await self._json(self._futures, "/api/v1/contract/detail")
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(data, list):
            raise RuntimeError("unexpected contract/detail response")
        return [row for row in data if isinstance(row, dict)]

    async def current_spot_assets(self) -> set[str]:
        payload = await self._json(self._spot, "/api/v3/exchangeInfo")
        if not isinstance(payload, dict):
            raise RuntimeError("unexpected spot exchangeInfo response")
        return parse_spot_usdt_assets(payload)

    async def kline_chunk(
        self, symbol: str, interval: str, start: datetime, end: datetime
    ) -> dict[str, Any]:
        payload = await self._json(
            self._futures,
            f"/api/v1/contract/kline/{symbol}",
            params={
                "interval": interval,
                "start": int(start.timestamp()),
                "end": int(end.timestamp()),
            },
        )
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(data, dict):
            raise RuntimeError(f"unexpected kline response for {symbol} {interval}")
        return data


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(microsecond=0)


def _chunk_ranges(start: datetime, end: datetime, interval: str, chunk_candles: int) -> Iterable[tuple[datetime, datetime]]:
    step = timedelta(seconds=INTERVAL_SECONDS[interval])
    span = step * chunk_candles
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + span - step)
        yield cursor, chunk_end
        cursor = chunk_end + step


def _chunk_path(cache_dir: Path, symbol: str, interval: str, start: datetime, end: datetime) -> Path:
    stamp = f"{int(start.timestamp())}-{int(end.timestamp())}.json.gz"
    return cache_dir / "candles" / interval / symbol / stamp


def _atomic_gzip_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _is_valid_cached_chunk(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 20:
        return False
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        return isinstance(payload, dict) and isinstance(payload.get("data"), dict)
    except (OSError, json.JSONDecodeError):
        return False


def _cache_size_bytes(cache_dir: Path) -> int:
    total = 0
    if not cache_dir.exists():
        return 0
    for root, _dirs, files in os.walk(cache_dir):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def _load_seed_symbols(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    result: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip().upper()
        if value and not value.startswith("#"):
            result.add(value)
    return result


class CacheLock:
    def __init__(self, cache_dir: Path) -> None:
        self.path = cache_dir / ".historical-fetch.lock"
        self.handle: Any = None

    def __enter__(self) -> "CacheLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another historical fetcher is already using {self.path.parent}") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return self

    def __exit__(self, *_args: object) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


async def _discover_universe(client: HistoricalPublicClient, config: HistoricalFetchConfig) -> list[str]:
    contracts, spot_assets = await asyncio.gather(client.current_contracts(), client.current_spot_assets())
    symbols = {
        str(row.get("symbol") or "").upper()
        for row in contracts
        if is_crypto_usdt_contract(row, spot_assets, require_spot_pair=True)
    }
    symbols.discard("")
    symbols.update(_load_seed_symbols(config.seed_symbols_file))
    ordered = sorted(symbols)
    if config.max_symbols is not None:
        ordered = ordered[: config.max_symbols]
    return ordered


async def fetch_history(config: HistoricalFetchConfig, *, dry_run: bool = False) -> RunStats:
    config.validate()
    stats = RunStats(started_at=datetime.now(UTC).isoformat())
    deadline = asyncio.get_running_loop().time() + config.max_runtime_minutes * 60.0
    max_bytes = int(config.max_cache_gb * (1024**3))

    with CacheLock(config.cache_dir):
        client = HistoricalPublicClient(config, stats)
        try:
            universe = await _discover_universe(client, config)
            stats.universe_symbols = len(universe)
            _atomic_json(
                config.cache_dir / "universe-latest.json",
                {
                    "captured_at": datetime.now(UTC).isoformat(),
                    "symbols": universe,
                    "note": "Current active universe plus optional seed symbols; historical delistings require seed input.",
                },
            )

            jobs: list[tuple[str, str, datetime, datetime, Path]] = []
            for symbol in universe:
                for interval in config.intervals:
                    for start, end in _chunk_ranges(config.start, config.end, interval, config.chunk_candles):
                        path = _chunk_path(config.cache_dir, symbol, interval, start, end)
                        jobs.append((symbol, interval, start, end, path))
            stats.planned_chunks = len(jobs)
            LOGGER.info(
                "Historical research plan: symbols=%d intervals=%s chunks=%d rate=%.2f req/s runtime_budget=%.1f min",
                len(universe),
                ",".join(config.intervals),
                len(jobs),
                config.requests_per_second,
                config.max_runtime_minutes,
            )
            if dry_run:
                stats.stop_reason = "dry_run"
                return stats

            for symbol, interval, start, end, path in jobs:
                if asyncio.get_running_loop().time() >= deadline:
                    stats.stop_reason = "runtime_budget_reached"
                    break
                if _cache_size_bytes(config.cache_dir) >= max_bytes:
                    stats.stop_reason = "cache_size_guard_reached"
                    break
                if _is_valid_cached_chunk(path):
                    stats.skipped_cached_chunks += 1
                    continue
                try:
                    data = await client.kline_chunk(symbol, interval, start, end)
                    count = len(data.get("time", [])) if isinstance(data.get("time"), list) else 0
                    record = {
                        "schema": 1,
                        "source": "MEXC public futures API",
                        "symbol": symbol,
                        "interval": interval,
                        "requested_start": start.isoformat(),
                        "requested_end": end.isoformat(),
                        "fetched_at": datetime.now(UTC).isoformat(),
                        "data": data,
                    }
                    _atomic_gzip_json(path, record)
                    stats.completed_chunks += 1
                    stats.fetched_candles += count
                except Exception as exc:  # preserve progress; never delete good cache files
                    stats.failed_chunks += 1
                    LOGGER.warning("Historical chunk failed %s %s %s..%s: %s", symbol, interval, start, end, exc)
                    if "throttling guard tripped" in str(exc):
                        stats.stop_reason = "throttling_guard"
                        break
            else:
                stats.stop_reason = "complete"
        finally:
            await client.close()
            stats.stopped_at = datetime.now(UTC).isoformat()
            _atomic_json(config.cache_dir / "last-run.json", asdict(stats))
    return stats


def audit_cache(cache_dir: Path) -> dict[str, Any]:
    files = list((cache_dir / "candles").glob("*/*/*.json.gz")) if (cache_dir / "candles").exists() else []
    valid = 0
    invalid: list[str] = []
    candles = 0
    by_interval: dict[str, int] = {}
    symbols: set[str] = set()
    for path in files:
        if not _is_valid_cached_chunk(path):
            invalid.append(str(path))
            continue
        valid += 1
        interval = path.parent.parent.name
        symbol = path.parent.name
        by_interval[interval] = by_interval.get(interval, 0) + 1
        symbols.add(symbol)
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
            times = payload.get("data", {}).get("time", [])
            candles += len(times) if isinstance(times, list) else 0
        except Exception:
            pass
    return {
        "cache_dir": str(cache_dir),
        "symbols": len(symbols),
        "valid_chunks": valid,
        "invalid_chunks": len(invalid),
        "candles": candles,
        "chunks_by_interval": by_interval,
        "size_bytes": _cache_size_bytes(cache_dir),
        "invalid_sample": invalid[:20],
    }


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return _utc(parsed)


def _default_start(months: int) -> datetime:
    # Calendar-exact month arithmetic is unnecessary for data collection; 183 days
    # intentionally covers just over six average months and is explicit/reproducible.
    return _utc(datetime.now(UTC) - timedelta(days=max(1, months) * 30 + 3))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Isolated, resumable historical MEXC research collector")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="download/resume historical public candles")
    fetch.add_argument("--cache-dir", default="research-history")
    fetch.add_argument("--months", type=int, default=6)
    fetch.add_argument("--start")
    fetch.add_argument("--end")
    fetch.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    fetch.add_argument("--rate", type=float, default=1.0, help="requests/sec; hard capped at 2")
    fetch.add_argument("--max-runtime-minutes", type=float, default=15.0)
    fetch.add_argument("--chunk-candles", type=int, default=1200)
    fetch.add_argument("--max-cache-gb", type=float, default=10.0)
    fetch.add_argument("--max-symbols", type=int)
    fetch.add_argument("--seed-symbols-file")
    fetch.add_argument("--dry-run", action="store_true")

    audit = sub.add_parser("audit", help="verify cached chunks without network or DB access")
    audit.add_argument("--cache-dir", default="research-history")
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    if args.command == "audit":
        print(json.dumps(audit_cache(Path(args.cache_dir)), indent=2, sort_keys=True))
        return 0

    end = _parse_dt(args.end) if args.end else _utc(datetime.now(UTC))
    start = _parse_dt(args.start) if args.start else _default_start(args.months)
    intervals = tuple(part.strip() for part in args.intervals.split(",") if part.strip())
    config = HistoricalFetchConfig(
        cache_dir=Path(args.cache_dir).expanduser().resolve(),
        start=start,
        end=end,
        intervals=intervals,
        requests_per_second=args.rate,
        max_runtime_minutes=args.max_runtime_minutes,
        chunk_candles=args.chunk_candles,
        max_cache_gb=args.max_cache_gb,
        max_symbols=args.max_symbols,
        seed_symbols_file=Path(args.seed_symbols_file).expanduser().resolve() if args.seed_symbols_file else None,
    )
    stats = await fetch_history(config, dry_run=args.dry_run)
    print(json.dumps(asdict(stats), indent=2, sort_keys=True))
    return 0


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    args = build_parser().parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())

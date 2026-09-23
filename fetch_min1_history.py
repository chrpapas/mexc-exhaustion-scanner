#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.mexc.com"
MAX_BARS_PER_REQUEST = 2000
DEFAULT_CHUNK_MINUTES = 1980


def dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        out = value
    else:
        out = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if out.tzinfo is None:
        out = out.replace(tzinfo=UTC)
    return out.astimezone(UTC)


def load_reference(path: Path) -> dict[str, Any]:
    opener = gzip.open if path.suffix.lower() == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        obj = json.load(handle)
    if not isinstance(obj, dict):
        raise RuntimeError(f"invalid production reference: {path}")
    return obj


def symbols_from_history(history: Path) -> list[str]:
    roots = [history / "candles" / "Min15", history / "live-store" / "contract" / "Min15"]
    out: set[str] = set()
    for root in roots:
        if root.is_dir():
            out.update(p.name.upper() for p in root.iterdir() if p.is_dir())
    return sorted(out)


class Pace:
    def __init__(self, requests_per_second: float) -> None:
        self.interval = 1.0 / max(0.1, requests_per_second)
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            sleep = max(0.0, self.next_at - now)
            if sleep:
                await asyncio.sleep(sleep)
            self.next_at = max(self.next_at, time.monotonic()) + self.interval


def chunk_windows(start: datetime, end: datetime, minutes: int):
    cursor = start.replace(second=0, microsecond=0)
    step = timedelta(minutes=minutes)
    while cursor < end:
        # end parameter is inclusive-ish; stop at the last minute open in chunk.
        chunk_end = min(end, cursor + step - timedelta(minutes=1))
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(minutes=1)


def validate_payload(payload: Any) -> int:
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected MEXC payload type: {type(payload).__name__}")
    if payload.get("success") is False:
        raise RuntimeError(f"MEXC error code={payload.get('code')} message={payload.get('message') or payload.get('msg')}")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise RuntimeError("MEXC kline payload has no data object")
    times = data.get("time") or []
    if not isinstance(times, list):
        raise RuntimeError("MEXC kline payload data.time is not a list")
    if len(times) > MAX_BARS_PER_REQUEST:
        raise RuntimeError(f"MEXC returned {len(times)} bars > documented max {MAX_BARS_PER_REQUEST}")
    return len(times)


async def main_async(args) -> int:
    history = Path(args.history).expanduser().resolve()
    ref = load_reference(Path(args.production_reference).expanduser().resolve())
    window = ref.get("window") or {}
    if args.start:
        start = dt(args.start)
    elif window.get("start"):
        start = dt(window["start"]) - timedelta(days=args.warmup_days)
    else:
        raise RuntimeError("no --start and production reference has no window.start")
    if args.end:
        end = dt(args.end)
    elif window.get("end"):
        end = dt(window["end"])
    else:
        raise RuntimeError("no --end and production reference has no window.end")
    if end <= start:
        raise RuntimeError("end must be after start")

    if args.symbols:
        symbols = sorted({x.strip().upper() for x in args.symbols.split(",") if x.strip()})
    else:
        symbols = symbols_from_history(history)
    if not symbols:
        raise RuntimeError("no symbols found; pass --symbols or verify history/candles/Min15")
    if args.max_symbols:
        symbols = symbols[: args.max_symbols]

    root = Path(args.output_root).expanduser().resolve() if args.output_root else history / "live-store" / "contract" / "Min1"
    root.mkdir(parents=True, exist_ok=True)

    pace = Pace(args.requests_per_second)
    sem = asyncio.Semaphore(args.concurrency)
    counters = {"downloaded": 0, "skipped": 0, "bars": 0, "empty": 0, "failed": 0}
    failures: list[str] = []

    timeout = httpx.Timeout(args.timeout)
    headers = {"User-Agent": "production-faithful-backtester-min1-fetch/1.2"}
    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), timeout=timeout, headers=headers) as client:
        async def fetch_one(symbol: str, a: datetime, b: datetime) -> None:
            symdir = root / symbol
            symdir.mkdir(parents=True, exist_ok=True)
            name = f"{a.strftime('%Y%m%dT%H%M%SZ')}__{b.strftime('%Y%m%dT%H%M%SZ')}.json.gz"
            path = symdir / name
            if path.exists() and path.stat().st_size > 40:
                counters["skipped"] += 1
                return
            async with sem:
                last_exc: Exception | None = None
                for attempt in range(1, args.retries + 1):
                    try:
                        await pace.wait()
                        response = await client.get(
                            f"/api/v1/contract/kline/{symbol}",
                            params={
                                "interval": "Min1",
                                "start": int(a.timestamp()),
                                "end": int(b.timestamp()),
                            },
                        )
                        response.raise_for_status()
                        payload = response.json()
                        n = validate_payload(payload)
                        tmp = path.with_suffix(path.suffix + ".tmp")
                        with gzip.open(tmp, "wt", encoding="utf-8") as handle:
                            json.dump(payload, handle, separators=(",", ":"))
                        tmp.replace(path)
                        counters["downloaded"] += 1
                        counters["bars"] += n
                        if n == 0:
                            counters["empty"] += 1
                        return
                    except Exception as exc:
                        last_exc = exc
                        await asyncio.sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))
                counters["failed"] += 1
                failures.append(f"{symbol} {a.isoformat()}..{b.isoformat()}: {last_exc}")

        jobs = [fetch_one(symbol, a, b) for symbol in symbols for a, b in chunk_windows(start, end, args.chunk_minutes)]
        total = len(jobs)
        print(f"Min1 fetch window: {start.isoformat()} -> {end.isoformat()}")
        print(f"symbols={len(symbols)} requests={total} root={root}")
        # Process in moderate batches to avoid creating tens of thousands of live tasks.
        batch = max(20, args.concurrency * 20)
        for i in range(0, total, batch):
            await asyncio.gather(*jobs[i:i+batch])
            done = min(total, i + batch)
            print(
                f"progress {done}/{total} downloaded={counters['downloaded']} "
                f"skipped={counters['skipped']} bars={counters['bars']} failed={counters['failed']}"
            )

    manifest = {
        "start": start.isoformat(), "end": end.isoformat(), "symbols": len(symbols),
        "root": str(root), **counters, "failures": failures,
    }
    (root / "FETCH-MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 2 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Resumable read-only MEXC Futures Min1 downloader for local backtest reconstruction")
    p.add_argument("--history", default="~/trader-backtest/research-history-v2")
    p.add_argument("--production-reference", required=True)
    p.add_argument("--output-root")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--warmup-days", type=int, default=10)
    p.add_argument("--symbols", help="comma-separated symbols; default derives from Min15 history")
    p.add_argument("--max-symbols", type=int)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--chunk-minutes", type=int, default=DEFAULT_CHUNK_MINUTES)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--requests-per-second", type=float, default=6.0)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--retries", type=int, default=5)
    return p


def main() -> int:
    args = build_parser().parse_args()
    if not (1 <= args.chunk_minutes <= 1999):
        raise SystemExit("--chunk-minutes must be 1..1999; MEXC documents max 2000 bars/request")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be >=1")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

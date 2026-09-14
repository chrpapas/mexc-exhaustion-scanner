from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.historical_research import (
    DEFAULT_INTERVALS,
    HistoricalFetchConfig,
    audit_cache,
    fetch_history,
    resolve_frozen_window,
)
from app.historical_universe import UniverseConfig, reconstruct_universe

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PipelineState:
    schema: int = 1
    created_at: str = ""
    updated_at: str = ""
    start: str = ""
    end: str = ""
    stage: str = "current_candles"
    current_batches: int = 0
    universe_batches: int = 0
    seeded_batches: int = 0
    stop_reason: str | None = None
    completed: bool = False


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(microsecond=0)


def _load_or_create_state(cache_dir: Path, months: int) -> tuple[PipelineState, datetime, datetime]:
    state_path = cache_dir / "historical-pipeline-state.json"
    start, end = resolve_frozen_window(cache_dir, months=months)
    if state_path.exists():
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            state = PipelineState(**{k: raw[k] for k in PipelineState.__dataclass_fields__ if k in raw})
            if state.start and state.end:
                frozen_start, frozen_end = _parse_iso(state.start), _parse_iso(state.end)
                if frozen_start != start or frozen_end != end:
                    raise RuntimeError(
                        "pipeline state window disagrees with research-window.json; use a new cache directory"
                    )
            return state, start, end
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid pipeline state at {state_path}: {exc}") from exc
    now = datetime.now(UTC).isoformat()
    state = PipelineState(created_at=now, updated_at=now, start=start.isoformat(), end=end.isoformat())
    _atomic_json(state_path, asdict(state))
    return state, start, end


def _save_state(cache_dir: Path, state: PipelineState) -> None:
    state.updated_at = datetime.now(UTC).isoformat()
    _atomic_json(cache_dir / "historical-pipeline-state.json", asdict(state))


def _terminal_fetch_reason(reason: str | None) -> bool:
    return reason == "complete"


def _terminal_universe_reason(reason: str | None) -> bool:
    return reason in {"reached_window_start", "announcement_pages_exhausted", "max_pages_reached"}


async def _run_fetch_until_complete(
    *,
    state: PipelineState,
    cache_dir: Path,
    start: datetime,
    end: datetime,
    intervals: tuple[str, ...],
    rate: float,
    batch_minutes: float,
    pause_seconds: float,
    max_cache_gb: float,
    seed_file: Path | None,
    counter_name: str,
) -> None:
    while True:
        config = HistoricalFetchConfig(
            cache_dir=cache_dir,
            start=start,
            end=end,
            intervals=intervals,
            requests_per_second=rate,
            max_runtime_minutes=batch_minutes,
            max_cache_gb=max_cache_gb,
            seed_symbols_file=seed_file,
        )
        stats = await fetch_history(config)
        setattr(state, counter_name, getattr(state, counter_name) + 1)
        _save_state(cache_dir, state)
        print(json.dumps({"stage": state.stage, **asdict(stats)}, indent=2, sort_keys=True))
        if _terminal_fetch_reason(stats.stop_reason):
            return
        if stats.stop_reason in {"throttling_guard", "cache_size_guard_reached"}:
            state.stop_reason = stats.stop_reason
            _save_state(cache_dir, state)
            raise RuntimeError(f"pipeline stopped safely: {stats.stop_reason}")
        if stats.stop_reason not in {"runtime_budget_reached", None}:
            state.stop_reason = stats.stop_reason
            _save_state(cache_dir, state)
            raise RuntimeError(f"historical fetch stopped unexpectedly: {stats.stop_reason}")
        await asyncio.sleep(max(0.0, pause_seconds))


async def run_pipeline(args: argparse.Namespace) -> int:
    cache_dir = Path(args.cache_dir).expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    state, start, end = _load_or_create_state(cache_dir, args.months)
    intervals = tuple(part.strip() for part in args.intervals.split(",") if part.strip())

    # Stage 1: current contract universe candles, fixed window, repeated internally.
    if state.stage == "current_candles":
        LOGGER.info("Pipeline stage 1/3: current-universe candles %s .. %s", start.isoformat(), end.isoformat())
        await _run_fetch_until_complete(
            state=state,
            cache_dir=cache_dir,
            start=start,
            end=end,
            intervals=intervals,
            rate=args.candle_rate,
            batch_minutes=args.batch_minutes,
            pause_seconds=args.pause_seconds,
            max_cache_gb=args.max_cache_gb,
            seed_file=None,
            counter_name="current_batches",
        )
        state.stage = "historical_universe"
        _save_state(cache_dir, state)

    # Stage 2: reconstruct delisted/historical crypto contracts. Cached website pages
    # make repeated 15-minute batches cheap and safe. Same cache lock prevents overlap.
    if state.stage == "historical_universe":
        LOGGER.info("Pipeline stage 2/3: reconstruct historical/delisted contract universe")
        signal_csvs = tuple(Path(v).expanduser().resolve() for v in args.signal_csv)
        while True:
            uconfig = UniverseConfig(
                cache_dir=cache_dir,
                start=start,
                end=end,
                requests_per_second=args.universe_rate,
                max_runtime_minutes=args.batch_minutes,
                max_pages=args.max_pages,
                signal_csvs=signal_csvs,
            )
            stats = await reconstruct_universe(uconfig)
            state.universe_batches += 1
            _save_state(cache_dir, state)
            print(json.dumps({"stage": state.stage, **asdict(stats)}, indent=2, sort_keys=True))
            if _terminal_universe_reason(stats.stop_reason):
                break
            if stats.stop_reason == "throttling_guard":
                state.stop_reason = "universe_throttling_guard"
                _save_state(cache_dir, state)
                raise RuntimeError("pipeline stopped safely after MEXC universe throttling/protection")
            if stats.stop_reason not in {"runtime_budget_reached", None}:
                state.stop_reason = stats.stop_reason
                _save_state(cache_dir, state)
                raise RuntimeError(f"historical-universe stage stopped unexpectedly: {stats.stop_reason}")
            await asyncio.sleep(max(0.0, args.pause_seconds))
        state.stage = "historical_seed_candles"
        _save_state(cache_dir, state)

    # Stage 3: same fixed candle window, now active + reconstructed delisted seed symbols.
    if state.stage == "historical_seed_candles":
        seed_file = cache_dir / "universe-history" / "historical-seed-symbols.txt"
        extras = []
        if seed_file.exists():
            extras = [line.strip() for line in seed_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        LOGGER.info("Pipeline stage 3/3: seeded historical candles, extra_symbols=%d", len(extras))
        if extras:
            await _run_fetch_until_complete(
                state=state,
                cache_dir=cache_dir,
                start=start,
                end=end,
                intervals=intervals,
                rate=args.candle_rate,
                batch_minutes=args.batch_minutes,
                pause_seconds=args.pause_seconds,
                max_cache_gb=args.max_cache_gb,
                seed_file=seed_file,
                counter_name="seeded_batches",
            )
        state.stage = "complete"
        state.completed = True
        state.stop_reason = "complete"
        _save_state(cache_dir, state)

    result = {
        "pipeline": asdict(state),
        "audit": audit_cache(cache_dir),
        "seed_file": str(cache_dir / "universe-history" / "historical-seed-symbols.txt"),
        "universe_evidence": str(cache_dir / "universe-history" / "historical-universe.json"),
    }
    _atomic_json(cache_dir / "historical-pipeline-final.json", result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="One-shot isolated historical research pipeline: active candles -> historical universe -> delisted candles"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run/resume all historical collection stages until safely complete")
    run.add_argument("--cache-dir", default="research-history-v2")
    run.add_argument("--months", type=int, default=6)
    run.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    run.add_argument("--candle-rate", type=float, default=1.0, help="public API req/s; collector hard cap remains 2")
    run.add_argument("--universe-rate", type=float, default=0.20, help="MEXC website req/s; universe hard cap remains 0.5")
    run.add_argument("--batch-minutes", type=float, default=15.0, help="internal safe checkpoint interval")
    run.add_argument("--pause-seconds", type=float, default=30.0, help="pause between internal batches")
    run.add_argument("--max-cache-gb", type=float, default=15.0)
    run.add_argument("--max-pages", type=int, default=80)
    run.add_argument("--signal-csv", action="append", default=[])
    return parser


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    args = build_parser().parse_args()
    if args.command != "run":
        raise RuntimeError(f"unsupported command {args.command}")
    return asyncio.run(run_pipeline(args))


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        out = value
    else:
        out = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if out.tzinfo is None:
        out = out.replace(tzinfo=UTC)
    return out.astimezone(UTC)


def jsonable(value: Any):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


async def export_reference(start: datetime, end: datetime, output: Path) -> dict[str, Any]:
    try:
        import asyncpg
    except ImportError as exc:
        raise RuntimeError("asyncpg is required; run this inside the Render scanner service environment") from exc

    from app.config import Settings
    from app.daily_core_strategy import daily_confirmed_core_v1_state
    from app.daily_bull_persistence_strategy import daily_bull_persistence_v2_state

    settings = Settings.from_env()
    warm_start = start - timedelta(hours=int(settings.episode_max_age_hours))
    conn = await asyncpg.connect(settings.database_url, command_timeout=120)
    try:
        episode_sql = """
            SELECT id, symbol, started_at, updated_at, state, peak_price, peak_at,
                   broken_level, breakdown_at, breakdown_atr_15m, retest_at,
                   confirmed_short_at, closed_at, last_run_score,
                   last_exhaustion_score, metadata
            FROM pump_episodes
            WHERE started_at <= $2
              AND (closed_at IS NULL OR closed_at > $1)
            ORDER BY id
        """
        cutoff_rows = [dict(r) for r in await conn.fetch(episode_sql, start, start)]
        timeline_rows = [dict(r) for r in await conn.fetch(episode_sql, warm_start, end)]

        sig_rows = [dict(r) for r in await conn.fetch(
            """
            SELECT rs.symbol, rs.signaled_at, rs.level, rs.score, rs.features,
                   rs.reasons, rs.episode_id,
                   st.entry_price, st.risk_tier,
                   pe.started_at AS episode_started_at,
                   pe.peak_at AS episode_peak_at,
                   pe.breakdown_at, pe.retest_at, pe.confirmed_short_at
            FROM run_signals rs
            LEFT JOIN shadow_trades st ON st.episode_id = rs.episode_id
            LEFT JOIN pump_episodes pe ON pe.id = rs.episode_id
            WHERE rs.level='confirmed_short'
              AND rs.signaled_at >= $1 AND rs.signaled_at <= $2
            ORDER BY rs.signaled_at, rs.symbol
            """,
            start,
            end,
        )]

        raw = []
        admitted = []
        statuses: Counter[str] = Counter()
        for row in sig_rows:
            features = row.get("features")
            if isinstance(features, str):
                try:
                    features = json.loads(features)
                except Exception:
                    features = {}
            if not isinstance(features, dict):
                features = {}

            core = daily_confirmed_core_v1_state(features)
            if core is None:
                status = "missing_core"
            elif core:
                status = "core_skip"
            else:
                persistence = daily_bull_persistence_v2_state(features)
                if persistence is None:
                    status = "missing_persistence"
                elif persistence:
                    status = "persistence_skip"
                else:
                    status = "admitted"
            statuses[status] += 1

            price = row.get("entry_price")
            if price is None:
                price = features.get("retest_close") or features.get("entry_price")
            risk = row.get("risk_tier") or features.get("risk_tier") or ""
            rec = {
                "symbol": row.get("symbol"),
                "signaled_at": row.get("signaled_at"),
                "entry_price": price,
                "risk_tier": risk,
                "status": status,
                "episode_id": row.get("episode_id"),
                "score": row.get("score"),
                "features": features,
                "reasons": row.get("reasons"),
            }
            raw.append(rec)
            if status == "admitted" and price is not None:
                admitted.append(rec)

        frozen = []
        try:
            frozen = [dict(r) for r in await conn.fetch(
                """
                SELECT * FROM research_signal_features
                WHERE confirmed_at >= $1 AND confirmed_at <= $2
                ORDER BY confirmed_at, symbol
                """,
                start,
                end,
            )]
        except Exception:
            frozen = []

        ticker_stats: dict[str, Any] = {}
        try:
            row = await conn.fetchrow(
                """
                WITH b AS (
                    SELECT observed_at, count(*)::int AS n
                    FROM ticker_snapshots
                    WHERE observed_at >= $1 AND observed_at <= $2
                    GROUP BY observed_at
                )
                SELECT count(*)::int AS buckets,
                       min(n)::int AS min_symbols,
                       max(n)::int AS max_symbols,
                       avg(n)::float8 AS avg_symbols
                FROM b
                """,
                start,
                end,
            )
            if row:
                ticker_stats = dict(row)
        except Exception:
            ticker_stats = {}

        cfg = asdict(settings)
        for key in list(cfg):
            low = key.lower()
            if "url" in low or "webhook" in low or "password" in low or "secret" in low or "token" in low:
                cfg[key] = "<redacted>" if cfg[key] else None

        payload = {
            "format": "trader-production-validation-reference-v2-dynamic-window",
            "created_at": datetime.now(UTC),
            "source_git_commit": os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT"),
            "window": {"start": start, "end": end, "warm_start": warm_start},
            "settings": cfg,
            "cutoff_active_episodes": cutoff_rows,
            "episode_timeline_rows": timeline_rows,
            "raw_signals": raw,
            "admitted_signals": admitted,
            "status_counts": dict(statuses),
            "research_signal_features": frozen,
            "ticker_snapshot_stats": ticker_stats,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(output, "wt", encoding="utf-8", compresslevel=9) as handle:
            json.dump(jsonable(payload), handle, separators=(",", ":"))
        return {
            "path": str(output),
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "cutoff_active_episodes": len(cutoff_rows),
            "episode_timeline_rows": len(timeline_rows),
            "raw_signals": len(raw),
            "admitted_signals": len(admitted),
            "status_counts": dict(statuses),
            "ticker_snapshot_stats": ticker_stats,
        }
    finally:
        await conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Read-only dynamic production reference export for forward validation")
    p.add_argument("--start", required=True, help="UTC/offset-aware ISO timestamp")
    p.add_argument("--end", required=True, help="UTC/offset-aware ISO timestamp")
    p.add_argument("--output", required=True, help=".json.gz output path")
    args = p.parse_args()
    start = parse_dt(args.start)
    end = parse_dt(args.end)
    if end <= start:
        raise SystemExit("--end must be after --start")
    info = asyncio.run(export_reference(start, end, Path(args.output).expanduser().resolve()))
    print(json.dumps(info, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

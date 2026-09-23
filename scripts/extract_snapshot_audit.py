from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Iterable

MARKER = "SNAPSHOT_AUDIT "


def iter_lines(path: str | None) -> Iterable[str]:
    if path is None or path == "-":
        yield from sys.stdin
        return
    with Path(path).expanduser().open("r", encoding="utf-8", errors="replace") as handle:
        yield from handle


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract structured SNAPSHOT_AUDIT records from Render/plain logs."
    )
    parser.add_argument("input", nargs="?", default="-", help="log file or - for stdin")
    parser.add_argument("--jsonl", required=True, help="output JSONL payload file")
    parser.add_argument("--csv", help="optional flattened row CSV")
    args = parser.parse_args()

    payloads: list[dict[str, object]] = []
    bad = 0
    for line in iter_lines(args.input):
        pos = line.find(MARKER)
        if pos < 0:
            continue
        raw = line[pos + len(MARKER) :].strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(payload, dict):
            payloads.append(payload)

    jsonl_path = Path(args.jsonl).expanduser()
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for payload in payloads:
            handle.write(json.dumps(payload, separators=(",", ":"), default=str) + "\n")

    flattened: list[dict[str, object]] = []
    for payload in payloads:
        rows = payload.get("rows")
        if not isinstance(rows, list):
            flattened.append(
                {
                    "event": payload.get("event"),
                    "cycle_started_at": payload.get("cycle_started_at"),
                    "row_count": payload.get("row_count"),
                    "payload": json.dumps(payload, separators=(",", ":"), default=str),
                }
            )
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            flattened.append(
                {
                    "event": payload.get("event"),
                    "cycle_started_at": payload.get("cycle_started_at"),
                    "chunk_index": payload.get("chunk_index"),
                    "chunk_count": payload.get("chunk_count"),
                    "kind": row.get("kind"),
                    "symbol": row.get("symbol"),
                    "queue_index": row.get("queue_index"),
                    "queue_size": row.get("queue_size"),
                    "row_json": json.dumps(row, separators=(",", ":"), default=str),
                }
            )

    if args.csv:
        csv_path = Path(args.csv).expanduser()
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            "event",
            "cycle_started_at",
            "chunk_index",
            "chunk_count",
            "kind",
            "symbol",
            "queue_index",
            "queue_size",
            "row_json",
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(flattened)

    print(
        f"audit payloads={len(payloads)} flattened_rows={len(flattened)} "
        f"bad_json_lines={bad} jsonl={jsonl_path}"
    )


if __name__ == "__main__":
    main()

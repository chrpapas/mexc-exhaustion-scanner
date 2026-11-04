"""External, read-only liveness inspection of the standalone pilot watchdog.

This is a one-shot sentinel check meant for a SEPARATE supervised process.
It does not start or restart the watchdog, notify Discord, open DB write,
call exchange APIs, or authorize trades. Healthy timestamps cannot prove
continuous or future liveness: an independent scheduler must invoke this
check and deliver alerts when it exits nonzero.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


@dataclass(frozen=True)
class WatchdogHeartbeat:
    status: str
    alert: bool = True
    age_ms: int | None = None
    revision: int | None = None
    unacknowledged_alarms: int | None = None
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def inspect_watchdog_heartbeat(
    path: str | Path, *, expected_scope_sha256: str,
    now_ms: int, stale_after_ms: int = 30000,
) -> WatchdogHeartbeat:
    """Bounded one-shot SQLite query-only check. Never creates a database."""
    def warn(status):
        return WatchdogHeartbeat(status)
    if (
        type(expected_scope_sha256) is not str
        or re.fullmatch(r"[a-f0-9]{64}", expected_scope_sha256) is None
        or type(now_ms) is not int or now_ms < 1_500_000_000_000
        or type(stale_after_ms) is not int or not 1000 <= stale_after_ms <= 300000
        or type(path) not in (str, Path) or not str(path) or str(path) == ":memory:"
    ):
        return warn("WATCH_HEARTBEAT_CHECK_CONFIG_INVALID")
    p = Path(path)
    try:
        # External liveness must not create missing journals or follow symlinks.
        if p.is_symlink() or p.parent.is_symlink():
            return warn("WATCH_HEARTBEAT_JOURNAL_UNSAFE")
        st = p.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            return warn("WATCH_HEARTBEAT_JOURNAL_UNSAFE")
        uri = "file:" + quote(str(p.resolve()), safe="/") + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=1000")
            row = db.execute(
                "SELECT scope_sha256, last_status, last_ms, "
                "alarm_latched, revision FROM state WHERE id=1"
            ).fetchone()
            pending = db.execute(
                "SELECT COUNT(*) FROM outbox WHERE delivered_ms IS NULL"
            ).fetchone()[0]
    except (OSError, sqlite3.Error, ValueError):
        return warn("WATCH_HEARTBEAT_JOURNAL_UNAVAILABLE")
    if (
        type(row) is not tuple or len(row) != 5
        or row[0] != expected_scope_sha256
    ):
        return warn("WATCH_HEARTBEAT_SCOPE_MISMATCH")
    _, last_status, last_ms, latch, revision = row
    if (
        type(last_status) is not str
        or re.fullmatch(r"WATCH_[A-Z0-9_]{1,90}", last_status) is None
        and last_status != "WATCH_NOT_STARTED"
        or type(last_ms) is not int or last_ms < 0
        or type(latch) is not int or latch not in (0, 1)
        or type(revision) is not int or revision < 0
        or type(pending) is not int or pending < 0
    ):
        return warn("WATCH_HEARTBEAT_STATE_INVALID")
    if revision == 0 or last_status == "WATCH_NOT_STARTED":
        return warn("WATCH_HEARTBEAT_NEVER_STARTED")
    if last_ms > now_ms + 2000:
        return warn("WATCH_HEARTBEAT_CLOCK_SKEW")
    age = max(0, now_ms - last_ms)
    detail = dict(age_ms=age, revision=revision, unacknowledged_alarms=pending)
    if age > stale_after_ms:
        return WatchdogHeartbeat("WATCH_HEARTBEAT_STALE", **detail)
    if latch or pending:
        return WatchdogHeartbeat("WATCH_HEARTBEAT_PREVIOUS_ALARM_LATCHED", **detail)
    if last_status not in {
        "WATCH_NATIVE_STOP_CURRENTLY_OBSERVED_NONAUTHORIZING",
        "WATCH_FLAT_INVENTORY_OBSERVED_NONAUTHORIZING",
    }:
        return WatchdogHeartbeat("WATCH_HEARTBEAT_LATEST_STATE_UNKNOWN", **detail)
    return WatchdogHeartbeat("WATCH_HEARTBEAT_RECENT_NONAUTHORIZING", alert=False, **detail)


def main():
    parser = argparse.ArgumentParser(description="GET-only external pilot heartbeat check")
    parser.add_argument("--journal", required=True)
    parser.add_argument("--expected-scope-sha256", required=True)
    parser.add_argument("--stale-after-ms", type=int, default=30000)
    args = parser.parse_args()
    result = inspect_watchdog_heartbeat(
        args.journal, expected_scope_sha256=args.expected_scope_sha256,
        now_ms=int(time.time() * 1000), stale_after_ms=args.stale_after_ms,
    )
    # The output is intentionally low-cardinality and contains no identities.
    print(result.status)
    print("READ-ONLY HEARTBEAT INSPECTION. NO EXCHANGE ORDERS.")
    sys.exit(2 if result.alert else 0)


if __name__ == "__main__":
    main()

"""Separate durable liveness sentinel for the GET-only T100 pilot watchdog.

READ-ONLY with respect to MEXC AND the watched watchdog journal. The sentinel
writes only its OWN separate local SQLite outbox. No exchange HTTP, keys,
execution clients, Render or trader imports. An external scheduler/process
supervisor must actually start and monitor this code.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from app.t100_pilot_watchdog_heartbeat import (
    WatchdogHeartbeat, inspect_watchdog_heartbeat,
)


class SentinelHalted(RuntimeError):
    """Low-cardinality error without private paths or exception messages."""


@dataclass(frozen=True)
class SentinelTick:
    status: str
    alarm_latched: bool
    notifications_pending: int
    notifications_delivered_this_tick: int
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    emergency_close_executed: bool = False
    read_only: bool = True


class PilotLivenessSentinel:
    """Independent one-host sentinel. Alarm delivery is at-least-once."""

    def __init__(
        self,
        *,
        watched_journal: str | Path,
        outbox_journal: str | Path,
        expected_scope_sha256: str,
        notifier: Callable[[str], Awaitable[bool]],
        stale_after_ms: int = 30000,
        poll_seconds: float = 5.0,
        notify_timeout_seconds: float = 5.0,
        clock_ms: Callable[[], int] | None = None,
        inspector: Callable[..., WatchdogHeartbeat] = inspect_watchdog_heartbeat,
    ):
        if (
            type(expected_scope_sha256) is not str
            or re.fullmatch(r"[a-f0-9]{64}", expected_scope_sha256) is None
            or type(stale_after_ms) is not int
            or not 1000 <= stale_after_ms <= 300000
            or any(type(v) not in (int, float) or not 0 < v <= 30
                   for v in (poll_seconds, notify_timeout_seconds))
            or not callable(notifier) or not callable(inspector)
            or not isinstance(watched_journal, (str, Path))
            or not isinstance(outbox_journal, (str, Path))
        ):
            raise SentinelHalted("SENTINEL_CONFIG_INVALID")
        watched = Path(watched_journal)
        outbox = Path(outbox_journal)
        if (
            not watched.is_absolute() or not outbox.is_absolute()
            or watched == outbox or str(watched) in ("", ":memory:")
            or str(outbox) in ("", ":memory:")
        ):
            raise SentinelHalted("SENTINEL_JOURNAL_SCOPE_INVALID")
        if outbox.is_symlink() or outbox.parent.is_symlink():
            raise SentinelHalted("SENTINEL_OUTBOX_JOURNAL_UNSAFE")
        self._watched = watched
        self._outbox = outbox
        self._expected_scope = expected_scope_sha256
        self._stale = stale_after_ms
        self._notifier = notifier
        self._inspector = inspector
        self._clock = clock_ms or (lambda: int(time.time() * 1000))
        self._poll = float(poll_seconds)
        self._notify_timeout = float(notify_timeout_seconds)
        self._db = None
        self._lock_fd = None
        self._closed = True
        # Bind the local alert journal to precisely one watched journal and
        # immutable sentinel scope. Not an authentication token.
        binding = hashlib.sha256(
            json.dumps([str(watched), expected_scope_sha256, stale_after_ms],
                       separators=(",", ":")).encode()
        ).hexdigest()
        old_file = outbox.exists()
        try:
            fd = os.open(
                str(outbox) + ".lock",
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            self._lock_fd = fd
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise SentinelHalted("SENTINEL_LOCK_FILE_UNSAFE")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fchmod(fd, 0o600)
            if outbox.is_symlink():
                raise SentinelHalted("SENTINEL_OUTBOX_JOURNAL_UNSAFE")
            if old_file:
                st = outbox.stat()
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    raise SentinelHalted("SENTINEL_OUTBOX_JOURNAL_UNSAFE")
            old_umask = os.umask(0o077)
            try:
                self._db = sqlite3.connect(str(outbox), isolation_level=None, timeout=1)
            finally:
                os.umask(old_umask)
            os.chmod(outbox, 0o600)
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS sentinel_state ("
                "id INTEGER PRIMARY KEY CHECK(id=1), binding TEXT NOT NULL,"
                "last_status TEXT NOT NULL, last_ms INTEGER NOT NULL,"
                "alarm_latched INTEGER NOT NULL, revision INTEGER NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS sentinel_outbox ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL,"
                "created_ms INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,"
                "delivered_ms INTEGER)"
            )
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT binding FROM sentinel_state WHERE id=1"
                ).fetchone()
                if row is None:
                    if old_file:
                        raise SentinelHalted("SENTINEL_PRIOR_SCOPE_MISSING")
                    self._db.execute(
                        "INSERT INTO sentinel_state VALUES (1, ?, 'WATCH_SENTINEL_NOT_STARTED', 0, 0, 0)",
                        (binding,),
                    )
                elif row[0] != binding:
                    raise SentinelHalted("SENTINEL_SCOPE_MISMATCH")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._closed = False
        except BaseException as exc:
            self.close()
            if isinstance(exc, SentinelHalted):
                raise
            raise SentinelHalted("SENTINEL_JOURNAL_OR_LOCK_UNAVAILABLE") from None

    def __enter__(self):
        if self._closed:
            raise SentinelHalted("SENTINEL_CLOSED")
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None
        self._closed = True

    def _now(self):
        try:
            value = self._clock()
        except Exception:
            raise SentinelHalted("SENTINEL_CLOCK_UNAVAILABLE") from None
        if type(value) is not int or value < 1_500_000_000_000:
            raise SentinelHalted("SENTINEL_CLOCK_UNAVAILABLE")
        return value

    def _observe(self, now):
        try:
            report = self._inspector(
                self._watched,
                expected_scope_sha256=self._expected_scope,
                stale_after_ms=self._stale,
                now_ms=now,
            )
        except Exception:
            return "WATCH_SENTINEL_HEARTBEAT_INSPECTION_FAILED", True
        if (
            type(report) is not WatchdogHeartbeat
            or type(report.status) is not str
            or re.fullmatch(r"WATCH_HEARTBEAT_[A-Z0-9_]{1,90}", report.status) is None
            or type(report.alert) is not bool
            or report.entry_allowed is not False
            or report.live_authorization is not False
            or report.mexc_orders_sent is not False
            or report.read_only is not True
        ):
            return "WATCH_SENTINEL_HEARTBEAT_INVALID", True
        healthy = report.status == "WATCH_HEARTBEAT_RECENT_NONAUTHORIZING"
        if healthy == report.alert:
            return "WATCH_SENTINEL_HEARTBEAT_INVALID", True
        return report.status, report.alert

    def _append(self, code, alert, at_ms):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            old_code, old_ms, latched, revision = self._db.execute(
                "SELECT last_status,last_ms,alarm_latched,revision FROM sentinel_state WHERE id=1"
            ).fetchone()
            if at_ms < old_ms:
                code, alert, at_ms = "WATCH_SENTINEL_CLOCK_REGRESSION", True, old_ms
            if alert and (not latched or old_code != code):
                self._db.execute(
                    "INSERT INTO sentinel_outbox(code,created_ms) VALUES(?,?)",
                    (code, at_ms),
                )
            self._db.execute(
                "UPDATE sentinel_state SET last_status=?,last_ms=?,alarm_latched=?,"
                "revision=? WHERE id=1",
                (code, at_ms, int(bool(latched or alert)), revision + 1),
            )
            self._db.execute("COMMIT")
            return code
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    async def poll_once(self) -> SentinelTick:
        if self._closed:
            raise SentinelHalted("SENTINEL_CLOSED")
        timestamp = self._now()
        status, alarm = self._observe(timestamp)
        try:
            status = self._append(status, alarm, timestamp)
        except Exception:
            raise SentinelHalted("SENTINEL_JOURNAL_WRITE_FAILED") from None
        delivered = 0
        pending = self._db.execute(
            "SELECT seq,code FROM sentinel_outbox WHERE delivered_ms IS NULL"
            " ORDER BY seq LIMIT 20"
        ).fetchall()
        for seq, code in pending:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                self._db.execute(
                    "UPDATE sentinel_outbox SET attempts=attempts+1 WHERE seq=?"
                    " AND delivered_ms IS NULL", (seq,),
                )
                self._db.execute("COMMIT")
                acknowledged = await asyncio.wait_for(
                    self._notifier(code), timeout=self._notify_timeout,
                )
                if acknowledged is not True:
                    break
                self._db.execute("BEGIN IMMEDIATE")
                self._db.execute(
                    "UPDATE sentinel_outbox SET delivered_ms=? WHERE seq=?"
                    " AND delivered_ms IS NULL", (self._now(), seq),
                )
                self._db.execute("COMMIT")
                delivered += 1
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                break
        row = self._db.execute(
            "SELECT alarm_latched FROM sentinel_state WHERE id=1"
        ).fetchone()
        count = self._db.execute(
            "SELECT COUNT(*) FROM sentinel_outbox WHERE delivered_ms IS NULL"
        ).fetchone()[0]
        return SentinelTick(
            status=status, alarm_latched=bool(row[0]),
            notifications_pending=count, notifications_delivered_this_tick=delivered,
        )

    async def run(self, *, max_cycles: int | None = 1) -> SentinelTick:
        if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
            raise SentinelHalted("SENTINEL_INVALID_CYCLES")
        cycle = 0
        while True:
            result = await self.poll_once()
            cycle += 1
            if max_cycles is not None and cycle >= max_cycles:
                return result
            await asyncio.sleep(self._poll)

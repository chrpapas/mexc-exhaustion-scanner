"""Durable, GET-only T100 pilot alarm-loop harness; never executes orders.

This is an offline-testable runner that requires an injected observer and
notification callback. It does NOT load credentials, start a service, or
implement an emergency close. Local single-host flock and SQLite FULL sync
are not distributed fencing; notifications are at-least-once, not exactly-once.
A crashed process has NO liveness monitor; a separate deployment/reviewer is
required before it can be treated as an operational watchdog.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import re
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from app.t100_pilot_watchdog_observer import PilotWatchVerdict


class WatchdogHalted(RuntimeError):
    """Public-safe status; never includes path or private venue exception."""


@dataclass(frozen=True)
class WatchdogTick:
    status: str
    alarm_latched: bool
    notifications_pending: int
    notifications_delivered_this_tick: int
    entry_allowed: bool = False
    live_authorization: bool = False
    emergency_close_executed: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


_SAFE_STATES = {
    "WATCH_NATIVE_STOP_CURRENTLY_OBSERVED_NONAUTHORIZING",
    "WATCH_FLAT_INVENTORY_OBSERVED_NONAUTHORIZING",
}


class PilotWatchdogRunner:
    """One owned pilot and one filesystem journal, no exchange mutation.

    The caller supplies an async GET-only observation returning PilotWatchVerdict
    and an async notification callback accepting ONLY a redacted WATCH_* code.
    The notification callback MUST NOT submit exchange orders. No such callback
    exists in this module.
    """

    def __init__(
        self,
        journal_path: str | Path,
        *,
        scope_sha256: str,
        observer: Callable[[], Awaitable[PilotWatchVerdict]],
        notifier: Callable[[str], Awaitable[bool]],
        poll_seconds: float = 1.0,
        observation_timeout_seconds: float = 5.0,
        notification_timeout_seconds: float = 5.0,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if (
            type(scope_sha256) is not str
            or re.fullmatch(r"[a-f0-9]{64}", scope_sha256) is None
            or not callable(observer) or not callable(notifier)
            or any(type(v) not in (int, float) or not 0 < v <= 30
                   for v in (poll_seconds, observation_timeout_seconds,
                             notification_timeout_seconds))
        ):
            raise WatchdogHalted("WATCHDOG_CONFIG_INVALID")
        if (
            not isinstance(journal_path, (str, Path))
            or not str(journal_path)
            or str(journal_path) == ":memory:"
        ):
            raise WatchdogHalted("WATCHDOG_DURABLE_PATH_REQUIRED")
        path = Path(journal_path)
        if path.is_symlink() or path.parent.is_symlink():
            raise WatchdogHalted("WATCHDOG_JOURNAL_PATH_UNSAFE")
        existing_journal = path.exists()
        self._observer = observer
        self._notifier = notifier
        self._clock = clock_ms or (lambda: int(time.time() * 1000))
        self._poll_seconds = float(poll_seconds)
        self._observation_timeout = float(observation_timeout_seconds)
        self._notification_timeout = float(notification_timeout_seconds)
        self._closed = True
        self._db = None
        self._lock_fd = None

        # Acquire process-exclusive lock before opening SQLite. No code path
        # breaks an existing lock or automatically resets an alarm.
        lock_path = path.with_name(path.name + ".lock")
        try:
            fd = os.open(
                lock_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            self._lock_fd = fd
            lock_stat = os.fstat(fd)
            if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
                raise WatchdogHalted("WATCHDOG_LOCK_FILE_UNSAFE")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fchmod(fd, 0o600)
            if path.is_symlink():
                raise WatchdogHalted("WATCHDOG_JOURNAL_PATH_UNSAFE")
            if path.exists():
                st = path.stat()
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    raise WatchdogHalted("WATCHDOG_JOURNAL_PATH_UNSAFE")
            old_umask = os.umask(0o077)
            try:
                self._db = sqlite3.connect(str(path), isolation_level=None, timeout=1.0)
            finally:
                os.umask(old_umask)
            os.chmod(path, 0o600)
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS state ("
                "id INTEGER PRIMARY KEY CHECK(id=1), scope_sha256 TEXT NOT NULL, "
                "last_status TEXT NOT NULL, last_ms INTEGER NOT NULL, "
                "alarm_latched INTEGER NOT NULL, revision INTEGER NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS outbox ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, "
                "created_ms INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
                "delivered_ms INTEGER)"
            )
            self._db.execute("BEGIN IMMEDIATE")
            try:
                existing = self._db.execute(
                    "SELECT scope_sha256 FROM state WHERE id=1"
                ).fetchone()
                if existing is None:
                    # A malformed preexisting database must never be rearmed.
                    if existing_journal:
                        raise WatchdogHalted("WATCHDOG_JOURNAL_MISSING_SCOPE_STATE")
                    self._db.execute(
                        "INSERT INTO state VALUES (1, ?, ?, 0, 0, 0)",
                        (scope_sha256, "WATCH_NOT_STARTED"),
                    )
                elif existing[0] != scope_sha256:
                    raise WatchdogHalted("WATCHDOG_SCOPE_MISMATCH")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._closed = False
        except BaseException as exc:
            self.close()
            if isinstance(exc, WatchdogHalted):
                raise
            raise WatchdogHalted("WATCHDOG_JOURNAL_OR_LOCK_UNAVAILABLE") from None

    def close(self) -> None:
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

    def __enter__(self):
        if self._closed:
            raise WatchdogHalted("WATCHDOG_CLOSED")
        return self

    def __exit__(self, *_):
        self.close()

    def _now(self) -> int:
        try:
            now = self._clock()
        except Exception:
            raise WatchdogHalted("WATCHDOG_CLOCK_UNAVAILABLE") from None
        if type(now) is not int or now < 1_500_000_000_000:
            raise WatchdogHalted("WATCHDOG_CLOCK_UNAVAILABLE")
        return now

    @staticmethod
    def _normalized_verdict(verdict: object) -> tuple[str, bool]:
        if (
            type(verdict) is not PilotWatchVerdict
            or type(verdict.status) is not str
            or re.fullmatch(r"WATCH_[A-Z0-9_]{1,90}", verdict.status) is None
            or type(verdict.alert_supervisor) is not bool
            or verdict.read_only is not True
            or verdict.entry_allowed is not False
            or verdict.live_authorization is not False
            or verdict.emergency_close_executed is not False
            or verdict.stop_execution_certified is not False
            or verdict.continuous_protection_certified is not False
            or verdict.mexc_orders_sent is not False
        ):
            return "WATCH_RUNTIME_VERDICT_INVALID", True
        is_safe = (
            verdict.status in _SAFE_STATES
            and verdict.alert_supervisor is False
            and (
                (verdict.status == "WATCH_NATIVE_STOP_CURRENTLY_OBSERVED_NONAUTHORIZING"
                 and verdict.currently_active_stop_observed is True
                 and verdict.account_flat_observed is False)
                or
                (verdict.status == "WATCH_FLAT_INVENTORY_OBSERVED_NONAUTHORIZING"
                 and verdict.account_flat_observed is True
                 and verdict.currently_active_stop_observed is False)
            )
        )
        if verdict.status in _SAFE_STATES and not is_safe:
            return "WATCH_RUNTIME_VERDICT_INVALID", True
        return verdict.status, not is_safe

    def _journal_observation(self, status: str, alarm: bool, now_ms: int) -> str:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            old_status, old_ms, old_latch, revision = self._db.execute(
                "SELECT last_status, last_ms, alarm_latched, revision "
                "FROM state WHERE id=1"
            ).fetchone()
            if now_ms < old_ms:
                status = "WATCH_RUNTIME_CLOCK_REGRESSION"
                alarm = True
                now_ms = old_ms
            if alarm and (not old_latch or old_status != status):
                self._db.execute(
                    "INSERT INTO outbox(code, created_ms) VALUES (?, ?)",
                    (status, now_ms),
                )
            self._db.execute(
                "UPDATE state SET last_status=?, last_ms=?, "
                "alarm_latched=?, revision=? WHERE id=1",
                (status, now_ms, int(bool(old_latch or alarm)), revision + 1),
            )
            self._db.execute("COMMIT")
            return status
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def _pending(self) -> list[tuple[int, str]]:
        return self._db.execute(
            "SELECT seq, code FROM outbox WHERE delivered_ms IS NULL "
            "ORDER BY seq ASC LIMIT 20"
        ).fetchall()

    async def poll_once(self) -> WatchdogTick:
        if self._closed:
            raise WatchdogHalted("WATCHDOG_CLOSED")
        try:
            verdict = await asyncio.wait_for(
                self._observer(), timeout=self._observation_timeout,
            )
            status, alarm = self._normalized_verdict(verdict)
        except Exception:
            status, alarm = "WATCH_RUNTIME_GET_TIMEOUT_OR_EXCEPTION", True
        now = self._now()
        try:
            status = self._journal_observation(status, alarm, now)
        except Exception:
            # A failed durable write is a fatal local safety failure.
            # Never run notifications as though the observation was retained.
            raise WatchdogHalted("WATCHDOG_DURABLE_OBSERVATION_FAILED") from None
        sent = 0
        for seq, code in self._pending():
            try:
                # Record the attempt BEFORE invoking any external notifier.
                self._db.execute("BEGIN IMMEDIATE")
                self._db.execute(
                    "UPDATE outbox SET attempts=attempts+1 WHERE seq=? "
                    "AND delivered_ms IS NULL", (seq,),
                )
                self._db.execute("COMMIT")
                acknowledged = await asyncio.wait_for(
                    self._notifier(code), timeout=self._notification_timeout,
                )
                if acknowledged is not True:
                    break
                self._db.execute("BEGIN IMMEDIATE")
                self._db.execute(
                    "UPDATE outbox SET delivered_ms=? WHERE seq=? "
                    "AND delivered_ms IS NULL", (self._now(), seq),
                )
                self._db.execute("COMMIT")
                sent += 1
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                break
        row = self._db.execute(
            "SELECT alarm_latched FROM state WHERE id=1"
        ).fetchone()
        pending = self._db.execute(
            "SELECT COUNT(*) FROM outbox WHERE delivered_ms IS NULL"
        ).fetchone()[0]
        return WatchdogTick(
            status=status, alarm_latched=bool(row[0]),
            notifications_pending=pending,
            notifications_delivered_this_tick=sent,
        )

    async def run(self, *, max_cycles: int | None = None) -> WatchdogTick:
        """Continuous observation loop only when caller explicitly starts it.

        No process manager, liveness alarm, endpoint or order executor exists.
        """
        if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
            raise WatchdogHalted("WATCHDOG_INVALID_CYCLE_LIMIT")
        cycles = 0
        while True:
            latest = await self.poll_once()
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return latest
            await asyncio.sleep(self._poll_seconds)

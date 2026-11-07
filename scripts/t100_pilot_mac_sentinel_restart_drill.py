#!/usr/bin/env python3
"""T100/225: bounded REAL launchd sentinel crash-and-restart drill, no exchange calls.

Only sends SIGKILL to the exact verified *sentinel* LaunchAgent via launchctl,
never the MEXC GET watcher. Does not write either SQLite journal or clear
persistent alarm latches. Requires both processes healthy and flat before
starting. launchd KeepAlive should restart the sentinel automatically.

Run on the intended Mac only: python3 t100_pilot_mac_sentinel_restart_drill.py
"""
from __future__ import annotations

import hashlib
import os
import plistlib
import re
import sqlite3
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

WATCH_LABEL = "com.t100225.pilot.getonly.watch"
SENTINEL_LABEL = "com.t100225.pilot.getonly.sentinel"
ROOT = Path.home() / "Library" / "Application Support" / "T100PilotMonitor"
STATE = ROOT / "state"
COPY = ROOT / "t100_pilot_mac_monitor.py"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"


class RestartDrillHalted(RuntimeError):
    """Only fixed, non-secret failure categories."""


def halt(code: str):
    raise RestartDrillHalted(code)


def _regular_owned(path: Path, *, allow_world_read: bool = False):
    try:
        st = path.lstat()
    except OSError:
        halt("INSTALLED_PATH_UNAVAILABLE")
    if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
            or st.st_uid != os.getuid() or st.st_mode & 0o022
            or (not allow_world_read and st.st_mode & 0o077)):
        halt("INSTALLED_PATH_UNSAFE")
    return path


def _verify_plists():
    """Confirm exact label, executable path and KeepAlive before process kill."""
    _regular_owned(COPY)
    for label, kind in ((WATCH_LABEL, "watch"), (SENTINEL_LABEL, "sentinel")):
        path = _regular_owned(LAUNCH_AGENTS / (label + ".plist"), allow_world_read=True)
        try:
            with path.open("rb") as f:
                entry = plistlib.load(f)
        except Exception:
            halt("LAUNCHD_PLIST_INVALID")
        argv = entry.get("ProgramArguments")
        if (entry.get("Label") != label
                or entry.get("KeepAlive") is not True
                or entry.get("RunAtLoad") is not True
                or type(argv) is not list or len(argv) != 3
                or type(argv[0]) is not str
                or not Path(argv[0]).is_absolute()
                or not Path(argv[0]).name.startswith("python")
                or argv[1:] != [str(COPY), kind]):
            halt("LAUNCHD_PLIST_SCOPE_INVALID")


def _run_launchctl(*args: str) -> str | None:
    try:
        p = subprocess.run(
            ["launchctl", *args], capture_output=True,
            timeout=8, text=True, check=False,
        )
        if p.returncode != 0:
            return None
        return p.stdout[:32768]
    except (OSError, subprocess.TimeoutExpired):
        return None


def _pid(label: str) -> int | None:
    target = "gui/%s/%s" % (os.getuid(), label)
    output = _run_launchctl("print", target)
    if output is None:
        return None
    found = re.findall(r"(?m)^\s*pid\s*=\s*([0-9]+)\s*$", output)
    if len(found) != 1:
        return None
    value = int(found[0])
    return value if value > 1 else None


def _kill_sentinel() -> bool:
    target = "gui/%s/%s" % (os.getuid(), SENTINEL_LABEL)
    return _run_launchctl("kill", "SIGKILL", target) is not None


def _kickstart_sentinel() -> bool:
    # A bounded recovery attempt if launchd automatic relaunch does not occur.
    target = "gui/%s/%s" % (os.getuid(), SENTINEL_LABEL)
    return _run_launchctl("kickstart", target) is not None


def _journal(path: Path) -> tuple | None:
    try:
        _regular_owned(path)
        uri = "file:" + quote(str(path), safe="/") + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as db:
            db.execute("PRAGMA query_only=ON")
            row = db.execute("SELECT binding,last_code,last_ms,revision,latched "
                             "FROM state WHERE id=1").fetchone()
            pending = db.execute("SELECT COUNT(*) FROM outbox WHERE delivered_ms IS NULL").fetchone()
        if row is None or pending is None or len(row) != 5:
            return None
        return row + (pending[0],)
    except (OSError, sqlite3.Error, RestartDrillHalted, ValueError):
        return None


@dataclass(frozen=True)
class Sample:
    watch_rev: int
    sentinel_rev: int


def _sample() -> Sample:
    if sys.platform != "darwin":
        halt("MACOS_REQUIRED")
    if _pid(WATCH_LABEL) is None or _pid(SENTINEL_LABEL) is None:
        halt("INSTALLED_AGENT_PROCESS_UNVERIFIED")
    _verify_plists()
    try:
        _regular_owned(STATE / "pilot_key.sha256")
        pin = (STATE / "pilot_key.sha256").read_text().strip()
    except (OSError, RestartDrillHalted):
        halt("PIN_NOT_VERIFIED")
    if re.fullmatch(r"[a-f0-9]{64}", pin) is None:
        halt("PIN_NOT_VERIFIED")
    sentinel_pin = hashlib.sha256(("SENTINEL:" + pin).encode()).hexdigest()
    now = int(time.time() * 1000)
    def good(path, expected_pin, expected_status, max_age):
        value = _journal(path)
        if value is None:
            halt("MONITOR_JOURNAL_NOT_VERIFIED")
        b, state, when, rev, latch, pending = value
        if (b != expected_pin or state != expected_status
                or type(when) is not int or type(rev) is not int or rev < 1
                or type(latch) is not int or latch != 0
                or type(pending) is not int or pending != 0):
            halt("MONITOR_NOT_FLAT_HEALTHY_OR_LATCHED")
        age = now - when
        if age < -2000 or age > max_age:
            halt("MONITOR_HEARTBEAT_STALE_OR_CLOCK_SKEW")
        return rev
    return Sample(
        good(STATE / "watch.sqlite", pin, "PILOT_FLAT_CONFIRMED", 75000),
        good(STATE / "sentinel.sqlite", sentinel_pin,
             "WATCHER_RECENT_FLAT_NONAUTHORIZING", 35000),
    )


def run_drill(*, sample=_sample, pid=_pid, send_kill=_kill_sentinel,
              kickstart=_kickstart_sentinel, now=time.monotonic, sleep=time.sleep,
              max_wait: float = 45.0, step: float = 1.0,
              emit=print) -> bool:
    """Return True only on a verified *automatic* relaunch with clean journals.

    A successful exit is never proof of continuous operation during the gap.
    """
    if not 5 <= max_wait <= 60 or not 0.05 <= step <= 5:
        halt("DRILL_TIMEOUT_INVALID")
    before = sample()
    old_pid = pid(SENTINEL_LABEL)
    if type(old_pid) is not int or old_pid <= 1:
        halt("SENTINEL_PID_NOT_VERIFIED")
    emit("PRECHECK=FLAT_BOTH_AGENTS_HEALTHY")
    emit("BEFORE_REVISIONS=WATCH_%s_SENTINEL_%s" % (before.watch_rev, before.sentinel_rev))
    emit("TARGET=ONLY_SENTINEL_LAUNCHAGENT")
    if send_kill() is not True:
        halt("SENTINEL_KILL_NOT_ACCEPTED")
    emit("SENTINEL_SIGNAL=SIGKILL_ACCEPTED")
    deadline = now() + max_wait
    try:
        while now() < deadline:
            sleep(step)
            next_pid = pid(SENTINEL_LABEL)
            if type(next_pid) is not int or next_pid <= 1 or next_pid == old_pid:
                continue
            try:
                after = sample()
            except RestartDrillHalted:
                continue
            if (after.sentinel_rev > before.sentinel_rev
                    and after.watch_rev >= before.watch_rev):
                emit("SENTINEL_AUTO_RESTART=PASS_NEW_PROCESS_AND_FRESH_JOURNAL")
                emit("AFTER_REVISIONS=WATCH_%s_SENTINEL_%s" % (
                    after.watch_rev, after.sentinel_rev))
                emit("ALARM_LATCHES=NONE_BEFORE_OR_AFTER")
                emit("INSTALLED_JOURNALS=READ_ONLY_BY_DRILL")
                emit("MEXC_REQUESTS_BY_DRILL=ZERO MEXC_MUTATIONS=ZERO LIVE_AUTHORIZATION=FALSE")
                return True
        halt("AUTO_RESTART_NOT_VERIFIED_WITHIN_BOUND")
    except BaseException:
        # Best effort: ask launchd to kickstart the *same sentinel* only.
        # This cannot erase latched alarms and is not proof of recovery.
        recovered = kickstart() is True
        emit("SENTINEL_RECOVERY_KICKSTART=" + ("ACCEPTED" if recovered else "FAILED"))
        raise


def main():
    try:
        run_drill()
        return 0
    except RestartDrillHalted as exc:
        print("T100_RESTART_DRILL_HALTED=" + str(exc))
    except Exception:
        print("T100_RESTART_DRILL_HALTED=UNEXPECTED_LOCAL_FAILURE")
    print("MEXC_MUTATIONS=ZERO LIVE_AUTHORIZATION=FALSE")
    return 2


if __name__ == "__main__":
    sys.exit(main())

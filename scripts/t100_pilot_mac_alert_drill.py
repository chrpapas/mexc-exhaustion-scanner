#!/usr/bin/env python3
"""T100/225 flat-pilot LIVE alert-delivery drill. No MEXC calls or orders.

This tool never modifies the installed monitor, launchd configuration or its
SQLite journals. It first verifies two independently updating launchd journals
and then exercises the identical local durable-outbox/Discord notification path
in *separate disposable journals*. The two Discord messages say DRILL ONLY.

Usage: python3 ~/Downloads/t100_pilot_mac_alert_drill.py
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
from pathlib import Path


class DrillHalted(RuntimeError):
    """Fixed safe error category (no secrets or paths)."""


def _halt(code: str):
    raise DrillHalted(code)


def _installed_monitor():
    if sys.platform != "darwin":
        _halt("MACOS_REQUIRED")
    source = (Path.home() / "Library" / "Application Support" / "T100PilotMonitor"
              / "t100_pilot_mac_monitor.py")
    try:
        st = source.lstat()
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or st.st_nlink != 1 or st.st_mode & 0o022):
            _halt("INSTALLED_MONITOR_PATH_UNSAFE")
        spec = importlib.util.spec_from_file_location("t100_pilot_drill_monitor", source)
        if spec is None or spec.loader is None:
            _halt("INSTALLED_MONITOR_UNAVAILABLE")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except DrillHalted:
        raise
    except Exception:
        _halt("INSTALLED_MONITOR_UNAVAILABLE")
    expected = ("Journal", "_read_state_readonly", "_read_pinned_binding", "_launchctl",
                "_keychain", "_valid_webhook", "_discord_post", "_binding_from_key")
    if any(not hasattr(module, attr) for attr in expected):
        _halt("INSTALLED_MONITOR_INCOMPATIBLE")
    return module


def _state(m, *, label: str, db: Path, binding: str, expected: str,
           max_age_ms: int, now_ms: int) -> int:
    if not m._launchctl("print", f"gui/{os.getuid()}/{label}"):
        _halt("MONITOR_AGENT_NOT_LOADED")
    result = m._read_state_readonly(db)
    if not isinstance(result, tuple) or len(result) != 6:
        _halt("MONITOR_JOURNAL_UNAVAILABLE")
    found_binding, code, millis, revision, latched, pending = result
    if (found_binding != binding or code != expected or type(millis) is not int
            or type(revision) is not int or revision < 1
            or latched != 0 or pending != 0):
        _halt("MONITOR_NOT_HEALTHY_OR_LATCHED")
    age = now_ms - millis
    if age < -2000 or age > max_age_ms:
        _halt("MONITOR_STALE_OR_CLOCK_SKEW")
    return revision


def _sample(m):
    try:
        binding = m._read_pinned_binding()
    except Exception:
        _halt("MONITOR_PIN_UNAVAILABLE")
    if not isinstance(binding, str) or re.fullmatch(r"[0-9a-f]{64}", binding) is None:
        _halt("MONITOR_PIN_INVALID")
    sentinel_binding = hashlib.sha256(("SENTINEL:" + binding).encode()).hexdigest()
    now = int(time.time() * 1000)
    return (
        _state(m, label=m.WATCH_LABEL, db=m.WATCH_DB, binding=binding,
               expected="PILOT_FLAT_CONFIRMED", max_age_ms=75000, now_ms=now),
        _state(m, label=m.SENTINEL_LABEL, db=m.SENTINEL_DB,
               binding=sentinel_binding, expected="WATCHER_RECENT_FLAT_NONAUTHORIZING",
               max_age_ms=35000, now_ms=now),
    )


def _webhooks(m):
    try:
        watch = m._keychain(m.MONITOR_WEBHOOK_SERVICE, "WEBHOOK_URL")
        sentinel = m._keychain(m.SENTINEL_WEBHOOK_SERVICE, "WEBHOOK_URL") or watch
    except Exception:
        _halt("DRILL_KEYCHAIN_UNAVAILABLE")
    if not watch or not sentinel or not m._valid_webhook(watch) or not m._valid_webhook(sentinel):
        _halt("DRILL_WEBHOOK_UNAVAILABLE")
    return watch, sentinel


def _pending(path: Path):
    with sqlite3.connect(str(path)) as db:
        rows = db.execute(
            "SELECT code,attempts,delivered_ms FROM outbox ORDER BY seq"
        ).fetchall()
    return rows


def _one_delivery_drill(m, *, tmp: Path, name: str, url: str,
                        label: str, code: str):
    # Names and label strings are static, locally controlled, and carry no
    # account/order/key data. Never point at production journal locations.
    path = tmp / (name + ".sqlite")
    binding = hashlib.sha256(("DRILL_TEMPORARY:" + name).encode()).hexdigest()
    old_sender = m._discord_post
    with m.Journal(path, binding) as journal:
        journal.record(code, healthy=False)
        # Simulated network rejection first. This MUST NOT contact Discord.
        try:
            m._discord_post = lambda *_args: False
            journal.notify(url, label)
        finally:
            m._discord_post = old_sender
    pending = _pending(path)
    if (len(pending) != 1 or pending[0][0] != code or pending[0][1] != 1
            or pending[0][2] is not None):
        _halt("DRILL_OUTBOX_DURABILITY_FAILED")
    # Re-open the same disposable journal to verify restart-safe retry,
    # then perform ONE actual Discord webhook request via production code.
    with m.Journal(path, binding) as journal:
        journal.notify(url, label)
    delivered = _pending(path)
    if (len(delivered) != 1 or delivered[0][1] != 2
            or type(delivered[0][2]) is not int):
        _halt("DRILL_DISCORD_NOT_CONFIRMED_BY_HTTP")
    with m.Journal(path, binding) as journal:
        journal.notify(url, label)
    if _pending(path) != delivered:
        _halt("DRILL_REPLAY_DUPLICATED_ALERT")
    return "HTTP_ACCEPTED_OUTBOX_ACKED"


def run(*, monitor=None, soak_seconds: float = 42.0, sleep=time.sleep):
    m = monitor or _installed_monitor()
    if not 0 <= soak_seconds <= 120:
        _halt("DRILL_INVALID_SOAK_INTERVAL")
    before = _sample(m)
    print("INSTALLED_AGENTS=LOADED_HEALTHY", flush=True)
    print("MONITOR_INITIAL_REVISIONS=WATCH_%d_SENTINEL_%d" % before, flush=True)
    if soak_seconds:
        sleep(soak_seconds)
    after = _sample(m)
    if soak_seconds and (after[0] <= before[0] or after[1] <= before[1]):
        _halt("MONITOR_CONTINUITY_NOT_VERIFIED")
    print("MONITOR_CONTINUITY=PASS_WATCH_AND_SENTINEL" if soak_seconds else
          "MONITOR_CONTINUITY=NOT_TESTED_ZERO_WAIT", flush=True)
    print("MONITOR_FINAL_REVISIONS=WATCH_%d_SENTINEL_%d" % after, flush=True)
    watch_url, sentinel_url = _webhooks(m)
    # Re-check journal health before any non-trading Discord traffic.
    _sample(m)
    with tempfile.TemporaryDirectory(prefix="t100-pilot-drill-") as td:
        path = Path(td)
        os.chmod(path, 0o700)
        try:
            a = _one_delivery_drill(
                m, tmp=path, name="watch-simulated-alert", url=watch_url,
                label="T100_PILOT_WATCH", code="DRILL_ONLY_WATCH_ALERT_TEST")
            print("DISCORD_WATCH_ALERT=" + a, flush=True)
            b = _one_delivery_drill(
                m, tmp=path, name="sentinel-simulated-alert", url=sentinel_url,
                label="T100_PILOT_SENTINEL", code="DRILL_ONLY_SENTINEL_ALERT_TEST")
            print("DISCORD_SENTINEL_ALERT=" + b, flush=True)
        except DrillHalted:
            raise
        except Exception:
            _halt("DRILL_LOCAL_JOURNAL_OR_DELIVERY_FAILED")
    _sample(m)
    print("PRODUCTION_MONITOR_JOURNALS=UNMODIFIED_BY_DRILL", flush=True)
    print("MEXC_REQUESTS_BY_DRILL=ZERO MEXC_MUTATIONS=ZERO LIVE_AUTHORIZATION=FALSE", flush=True)
    print("DISCORD_CHANNEL_VISIBILITY=OPERATOR_CONFIRMATION_REQUIRED", flush=True)


def main():
    parser = argparse.ArgumentParser(description="GET-free T100 pilot Mac alert pathway drill")
    parser.add_argument("--soak-seconds", type=float, default=42.0,
                        help="Seconds to verify two watcher and sentinel heartbeats (default 42)")
    args = parser.parse_args()
    try:
        run(soak_seconds=args.soak_seconds)
    except DrillHalted as err:
        print("T100_DRILL_HALTED=" + str(err), flush=True)
        print("MEXC_MUTATIONS=ZERO LIVE_AUTHORIZATION=FALSE", flush=True)
        return 2
    except Exception:
        print("T100_DRILL_HALTED=UNEXPECTED_LOCAL_FAILURE", flush=True)
        print("MEXC_MUTATIONS=ZERO LIVE_AUTHORIZATION=FALSE", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
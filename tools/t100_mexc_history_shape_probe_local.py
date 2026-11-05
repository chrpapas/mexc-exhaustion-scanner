"""One-shot MEXC historical-position API shape probe on the local Mac.

Purpose: settle the published schema contradiction (metadata object vs array).
Dedicated T100 pilot subaccount ONLY; credentials fetched from macOS Keychain.
Exactly ONE signed HTTPS GET. No POST, cancel, DB, Render or order capability.

Output is a fixed set of booleans/counts/field-presence flags. It never
prints exchange IDs, prices, timestamps, row payloads, credentials or URL
headers. Does not prove history completeness or authorize live trading.

Run on Mac:
    python3 t100_mexc_history_shape_probe_local.py --diagnose

Requires Keychain generic password items:
  service=t100225-mexc-pilot-subaccount
  account=MEXC_API_KEY / MEXC_API_SECRET
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import platform
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

SERVICE = "t100225-mexc-pilot-subaccount"
BASE = "https://api.mexc.com"
PATH = "/api/v1/private/position/list/history_positions"
SYMBOL = "BTC_USDT"


class ProbeHalt(RuntimeError):
    pass


def _read_keychain(account: str) -> str:
    if account not in ("MEXC_API_KEY", "MEXC_API_SECRET"):
        raise ProbeHalt("KEYCHAIN_ACCOUNT_NOT_ALLOWLISTED")
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE,
             "-a", account, "-w"],
            check=True, capture_output=True, text=True, timeout=20,
        ).stdout.strip()
    except Exception:
        raise ProbeHalt("KEYCHAIN_LOOKUP_FAILED") from None
    if not out:
        raise ProbeHalt("KEYCHAIN_ITEM_EMPTY")
    return out


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: Any,
                         msg: Any, headers: Any, newurl: Any) -> None:
        raise ProbeHalt("CREDENTIALLED_REDIRECT_PROHIBITED")


def _get_history_first_page(*, key: str, secret: str,
                            opener: Any = None, stamp_ms: int | None = None) -> Any:
    if (type(key) is not str or not key or
            type(secret) is not str or not secret):
        raise ProbeHalt("MISSING_API_KEY_OR_SECRET")
    if stamp_ms is None:
        stamp_ms = int(time.time() * 1000)
    if type(stamp_ms) is not int or stamp_ms <= 0:
        raise ProbeHalt("INVALID_REQUEST_TIMESTAMP")
    # The current official Futures API host replaced contract.mexc.com in
    # January 2026. No retired-host fallback or credentialed redirects.
    query = urllib.parse.urlencode(sorted([
        ("symbol", SYMBOL), ("page_num", 1), ("page_size", 20),
    ]))
    signed = f"{key}{stamp_ms}{query}"
    sig = hmac.new(secret.encode("utf-8"), signed.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        BASE + PATH + "?" + query,
        headers={
            "ApiKey": key,
            "Request-Time": str(stamp_ms),
            "Signature": sig,
            "Recv-Window": "10",
            "Language": "en-US",
        },
        method="GET",
    )
    http = opener if opener is not None else urllib.request.build_opener(
        _RejectRedirects()
    )
    try:
        with http.open(req, timeout=12) as res:
            payload = json.loads(res.read(512 * 1024))
    except ProbeHalt:
        raise
    except urllib.error.HTTPError:
        raise ProbeHalt("MEXC_HTTP_GET_REJECTED") from None
    except Exception:
        raise ProbeHalt("MEXC_GET_OR_JSON_FAILED") from None
    if not isinstance(payload, dict):
        raise ProbeHalt("MEXC_ENVELOPE_NOT_OBJECT")
    if payload.get("success") is not True or payload.get("code") != 0:
        raise ProbeHalt("MEXC_AUTHENTICATED_GET_REJECTED")
    return payload.get("data")


def summarize_shape(data: Any) -> dict[str, Any]:
    """No raw row values. One-page structural evidence only."""
    if type(data) is list:
        rows = data
        shape = "ARRAY_WITHOUT_PAGINATION_METADATA"
        meta = {}
    elif type(data) is dict:
        rows = data.get("resultList")
        shape = ("OBJECT_WITH_RESULT_LIST" if type(rows) is list
                 else "OBJECT_WITHOUT_RESULT_LIST")
        meta = data
    else:
        rows = None
        meta = {}
        shape = "UNRECOGNIZED"
    if rows is not None and (
            len(rows) > 20 or any(type(item) is not dict for item in rows)):
        raise ProbeHalt("MEXC_ROW_SHAPE_UNVERIFIED")
    wanted = (
        "positionId","symbol","positionType","openType","state",
        "holdVol","closeVol","positionShowStatus",
    )
    present = (
        {field: all(field in row for row in rows)
         for field in wanted} if rows else
        {field: False for field in wanted}
    )
    pagination = {
        name: type(meta.get(name)) is int
        for name in ("pageSize","totalCount","totalPage","currentPage")
    }
    return {
        "FORENSIC_MODE": "ONE_SHOT_SIGNED_GET_ONLY",
        "MEXC_FUTURES_HOST": "api.mexc.com",
        "DATA_SHAPE": shape,
        "FIRST_PAGE_ROWS": len(rows) if rows is not None else "UNVERIFIED",
        "PAGINATION_METADATA_TYPES_VALID": all(pagination.values()),
        "PAGINATION_FIELDS_PRESENT": pagination,
        "POSITION_ROW_FIELDS_PRESENT": present,
        "PAGE_ONE_ONLY": True,
        "FULL_HISTORY_CERTIFIED": False,
        "POSITION_CLOSURE_CERTIFIED": False,
        "LIVE_AUTHORIZATION": False,
        "EXCHANGE_MUTATIONS": 0,
        "DATABASE_MUTATIONS": 0,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="T100 local history schema GET probe")
    p.add_argument("--diagnose", action="store_true")
    args = p.parse_args()
    if not args.diagnose:
        print("PROBE_MODE=OFFLINE_NO_NETWORK; use --diagnose on the local Mac")
        return 0
    try:
        if platform.system() != "Darwin":
            raise ProbeHalt("LOCAL_MAC_REQUIRED")
        key = _read_keychain("MEXC_API_KEY")
        secret = _read_keychain("MEXC_API_SECRET")
        result = summarize_shape(
            _get_history_first_page(key=key, secret=secret)
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except ProbeHalt as exc:
        # Controlled code only. Never include exception from subprocess,
        # urllib, external JSON, or MEXC response.
        print(json.dumps({
            "FORENSIC_MODE": "ONE_SHOT_SIGNED_GET_ONLY",
            "STATUS": str(exc),
            "EXCHANGE_MUTATIONS": 0,
            "LIVE_AUTHORIZATION": False,
        }, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

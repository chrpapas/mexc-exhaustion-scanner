"""V5 T100 real pilot: exact closed-position forensic check, GET ONLY.

Standalone macOS script. Reads existing V5 journal (never edits) and
existing dedicated T100 pilot credentials from macOS Keychain.
Performs exactly FOUR signed GET requests on api.mexc.com:
  two first-page BTC_USDT historical-position array reads and
  two exact-position-ID open-position reads.
No order placement, no DELETE/POST, no DB or Render change, no retries.

Redacted output: never prints API keys, exchange IDs, prices, PnL, raw
history, timestamps or journal contents. Does not certify whole history,
whole account flatness, native stop triggerSide, or live authorization.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

JOURNAL = Path.home() / "Downloads" / "t100_real_stop_trigger_v5_DO_NOT_DELETE.json"
LOCK = Path.home() / "Downloads" / "t100_real_stop_trigger_v5.lock"
SERVICE = "t100225-mexc-pilot-subaccount"
HOST = "https://api.mexc.com"
HISTORY = "/api/v1/private/position/list/history_positions"
OPEN = "/api/v1/private/position/open_positions"
SYMBOL = "BTC_USDT"


class AuditHalt(RuntimeError):
    pass


def _journal(path: Path = JOURNAL) -> dict[str, Any]:
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os,"O_NOFOLLOW",0))
    except OSError:
        raise AuditHalt("V5_JOURNAL_NOT_FOUND") from None
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or not 0 < meta.st_size <= 65536:
            raise AuditHalt("V5_JOURNAL_UNSAFE")
        raw = os.read(fd, 65537)
        row = json.loads(raw)
    except (OSError, ValueError, TypeError):
        raise AuditHalt("V5_JOURNAL_UNREADABLE") from None
    finally:
        os.close(fd)
    if type(row) is not dict:
        raise AuditHalt("V5_JOURNAL_SHAPE_INVALID")
    oid = row.get("entry_oid")
    pid = row.get("position_id")
    created = row.get("created_utc_ms")
    if (type(oid) is not str or
            re.fullmatch(r"t100v4_[a-f0-9]{12}_entry",oid) is None or
            row.get("close_oid") != oid[:-5]+"close" or
            type(pid) not in (str,int) or
            not re.fullmatch(r"[1-9][0-9]*",str(pid)) or
            type(created) is not int or created <= 0 or
            created > int(time.time()*1000)+60000):
        raise AuditHalt("V5_JOURNAL_OWNERSHIP_UNVERIFIED")
    # Do not rely on any journal "flat" flag as exchange evidence.
    return row


def _keychain(account: str) -> str:
    if account not in ("MEXC_API_KEY","MEXC_API_SECRET"):
        raise AuditHalt("KEYCHAIN_ITEM_NOT_ALLOWED")
    try:
        proc = subprocess.run(
            ["security","find-generic-password","-s",SERVICE,
             "-a",account,"-w"],
            check=True,capture_output=True,text=True,timeout=15,
        )
    except Exception:
        raise AuditHalt("KEYCHAIN_READ_FAILED") from None
    value=proc.stdout.strip()
    if not value:
        raise AuditHalt("KEYCHAIN_VALUE_EMPTY")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AuditHalt("SIGNED_GET_REDIRECT_REFUSED")


def _get(*, path: str, params: dict[str,int|str],
         key: str, secret: str, opener=None, stamp_ms: int|None=None):
    if path not in (HISTORY,OPEN):
        raise AuditHalt("GET_ENDPOINT_NOT_ALLOWLISTED")
    if path == HISTORY:
        if params != dict(symbol=SYMBOL,page_num=1,page_size=100):
            raise AuditHalt("HISTORY_QUERY_NOT_ALLOWLISTED")
    elif (set(params) != {"positionId"} or
          type(params["positionId"]) is not int or params["positionId"] <= 0):
        raise AuditHalt("EXACT_POSITION_QUERY_NOT_ALLOWLISTED")
    if not key or not secret:
        raise AuditHalt("GET_CREDENTIALS_MISSING")
    stamp = int(time.time()*1000) if stamp_ms is None else stamp_ms
    if type(stamp) is not int or stamp <= 0:
        raise AuditHalt("INVALID_TIMESTAMP")
    query = urlencode(sorted(params.items()))
    signature = hmac.new(
        secret.encode(),f"{key}{stamp}{query}".encode(),hashlib.sha256,
    ).hexdigest()
    request=Request(
        HOST+path+"?"+query,
        headers={"ApiKey":key,"Request-Time":str(stamp),
                 "Signature":signature,"Recv-Window":"10","Language":"en-US"},
        method="GET",
    )
    try:
        transport = opener if opener is not None else build_opener(_NoRedirect())
        with transport.open(request,timeout=12) as response:
            payload=json.loads(response.read(524288))
    except AuditHalt:
        raise
    except Exception:
        raise AuditHalt("SIGNED_GET_FAILED") from None
    if (type(payload) is not dict or
          payload.get("success") is not True or
          type(payload.get("code")) is not int or payload["code"] != 0):
        raise AuditHalt("SIGNED_GET_MEXC_NOT_SUCCESS")
    return payload.get("data")


def _dec(v):
    if v is None or type(v) is bool:
        return None
    try:
        d=Decimal(str(v))
        return d if d.is_finite() else None
    except (InvalidOperation, TypeError,ValueError):
        return None


def _history_exact(rows: Any, pid: str):
    """Return (shape verified, exact terminal matching row) without PII."""
    if type(rows) is not list or len(rows)>100:
        return False,None
    seen=set()
    exact=None
    for row in rows:
        if type(row) is not dict:
            return False,None
        rid=str(row.get("positionId") or "")
        if (not re.fullmatch(r"[1-9][0-9]*",rid) or
                rid in seen or row.get("symbol") != SYMBOL):
            return False,None
        seen.add(rid)
        if rid == pid:
            exact=row
    return True,exact


def _terminal(row, pid: str) -> bool:
    return bool(
        type(row) is dict and
        str(row.get("positionId")) == pid and
        row.get("symbol") == SYMBOL and
        type(row.get("positionType")) is int and row["positionType"] == 2 and
        type(row.get("openType")) is int and row["openType"] == 1 and
        type(row.get("state")) is int and row["state"] == 3 and
        _dec(row.get("holdVol")) == Decimal(0) and
        _dec(row.get("closeVol")) == Decimal(1) and
        row.get("positionShowStatus") == "CLOSED"
    )


def classify(journal, first_history, second_history,
             first_open, second_open) -> dict[str, Any]:
    """Positive matching closed position only; no inference from absence."""
    pid=str(journal["position_id"])
    valid1, row1 = _history_exact(first_history,pid)
    valid2, row2 = _history_exact(second_history,pid)
    if not valid1 or not valid2:
        status="HISTORY_ROWS_UNVERIFIED"
    elif type(first_open) is not list or type(second_open) is not list:
        status="EXACT_POSITION_GET_SHAPE_UNVERIFIED"
    elif any(type(x) is not dict or str(x.get("positionId"))!=pid
             for x in first_open+second_open):
        status="EXACT_POSITION_FILTER_UNVERIFIED"
    elif first_open or second_open:
        status="POSITION_STILL_OPEN_OR_REAPPEARED"
    elif row1 is None or row2 is None:
        status="V5_POSITION_NOT_ON_BOTH_FIRST_PAGES_NOT_PROVEN"
    elif not _terminal(row1,pid) or not _terminal(row2,pid):
        status="V5_HISTORY_RECORD_NOT_FULLY_CLOSED_VERIFIED"
    elif any(str(row1.get(f)) != str(row2.get(f))
             for f in ("positionId","symbol","positionType","openType",
                       "state","holdVol","closeVol","positionShowStatus")):
        status="V5_HISTORY_MATCH_DRIFT"
    else:
        status="V5_EXACT_CLOSED_POSITION_POSITIVE_HISTORY_MATCH"
    return {
        "FORENSIC_MODE":"EXISTING_V5_JOURNAL_FOUR_SIGNED_GET_ONLY",
        "V5_EXACT_CLOSED_POSITION_HISTORY":status,
        "HISTORY_RESPONSE":"ARRAY_WITHOUT_PAGINATION_METADATA" if valid1 and valid2
                              else "UNVERIFIED",
        "V5_CLOSED_POSITION_VERIFIED":status=="V5_EXACT_CLOSED_POSITION_POSITIVE_HISTORY_MATCH",
        "FULL_HISTORY_CERTIFIED":False,
        "WHOLE_ACCOUNT_FLAT_CERTIFIED":False,
        "STOP_LOSS_TRIGGER_SIDE_PROVEN":False,
        "LIVE_AUTHORIZATION":False,
        "MEXC_ORDERS_SENT":0,
        "DATABASE_MUTATIONS":0,
        "REQUIRES_MANUAL_POSITION_MANAGEMENT_CHANGE":False,
    }


def main() -> int:
    parser=argparse.ArgumentParser(description="V5 exact closed position, GET only")
    parser.add_argument("--diagnose",action="store_true")
    args=parser.parse_args()
    if not args.diagnose:
        print("MODE=OFFLINE_NO_NETWORK; use --diagnose on Mac")
        return 0
    os.umask(0o077)
    try:
        if platform.system()!="Darwin":
            raise AuditHalt("LOCAL_MAC_REQUIRED")
        with open(LOCK,"a+") as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            journal=_journal()
            key=_keychain("MEXC_API_KEY")
            secret=_keychain("MEXC_API_SECRET")
            pid=int(str(journal["position_id"]))
            a=_get(path=HISTORY,params=dict(symbol=SYMBOL,page_num=1,page_size=100),
                   key=key,secret=secret)
            p=_get(path=OPEN,params=dict(positionId=pid),key=key,secret=secret)
            b=_get(path=HISTORY,params=dict(symbol=SYMBOL,page_num=1,page_size=100),
                   key=key,secret=secret)
            q=_get(path=OPEN,params=dict(positionId=pid),key=key,secret=secret)
            print(json.dumps(classify(journal,a,b,p,q),sort_keys=True))
            return 0
    except (AuditHalt,BlockingIOError) as exc:
        code=str(exc) if type(exc) is AuditHalt else "V5_PILOT_LOCK_BUSY"
        print(json.dumps({
            "FORENSIC_STATUS":code,
            "MEXC_ORDERS_SENT":0,
            "LIVE_AUTHORIZATION":False,
        },sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({
            "FORENSIC_STATUS":"UNEXPECTED_READ_ONLY_FAILURE",
            "MEXC_ORDERS_SENT":0,"LIVE_AUTHORIZATION":False,
        },sort_keys=True))
        return 2


if __name__=="__main__":
    raise SystemExit(main())

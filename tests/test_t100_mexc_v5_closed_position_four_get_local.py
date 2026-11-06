"""Offline safety regressions for the four signed-GET V5 Mac journal check.

No real exchange requests, DB writes, or order mutation. Redacted results.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qsl, urlsplit

from tools.t100_mexc_v5_closed_position_four_get_local import (
    _get,_journal,_history_exact,_terminal, classify,
    AuditHalt,HISTORY,OPEN,
)


def journal():
    return {
        "entry_oid":"t100v4_abcdef123456_entry",
        "close_oid":"t100v4_abcdef123456_close",
        "position_id":"555",
        "stop_order_id":"777",
        "created_utc_ms":1762370000000,
    }


def historical(**kw):
    row=dict(positionId=555,symbol="BTC_USDT",positionType=2,
             openType=1,state=3,holdVol=0,closeVol=1,
             positionShowStatus="CLOSED")
    row.update(kw)
    return row


class Reply:
    def __init__(self,data):
        self.data=data

    def __enter__(self):
        return self

    def __exit__(self,*_args):
        return False

    def read(self,max_bytes=0):
        return json.dumps(dict(success=True,code=0,data=self.data)).encode()


class Opener:
    def __init__(self,data):
        self.data=data
        self.calls=[]

    def open(self,req,timeout=None):
        self.calls.append((req,timeout))
        return Reply(self.data)


class ForensicTests(unittest.TestCase):
    def test_four_get_policy_positive_exact_history_only(self):
        a=[historical(positionId=551),historical(positionId=552),
           historical(),historical(positionId=554)]
        result=classify(journal(),a,a,[],[])
        self.assertEqual(result["V5_EXACT_CLOSED_POSITION_HISTORY"],
                         "V5_EXACT_CLOSED_POSITION_POSITIVE_HISTORY_MATCH")
        self.assertTrue(result["V5_CLOSED_POSITION_VERIFIED"])
        self.assertFalse(result["FULL_HISTORY_CERTIFIED"])
        self.assertFalse(result["WHOLE_ACCOUNT_FLAT_CERTIFIED"])
        self.assertFalse(result["STOP_LOSS_TRIGGER_SIDE_PROVEN"])
        self.assertFalse(result["LIVE_AUTHORIZATION"])
        self.assertEqual(result["MEXC_ORDERS_SENT"],0)
        self.assertNotIn("555",json.dumps(result))
        self.assertNotIn("777",json.dumps(result))

    def test_missing_exact_record_is_not_proof_of_closure(self):
        r=classify(journal(),[historical(positionId=551)],
                   [historical(positionId=551)],[],[])
        self.assertFalse(r["V5_CLOSED_POSITION_VERIFIED"])
        self.assertIn("NOT_PROVEN",r["V5_EXACT_CLOSED_POSITION_HISTORY"])

    def test_any_still_open_result_blocks_closed_cert(self):
        for present in (
            ([dict(positionId=555)],[]),
            ([],[dict(positionId=555)]),
            ([dict(positionId=555)],[dict(positionId=555)]),
        ):
            with self.subTest(present=present):
                r=classify(journal(),[historical()],[historical()],*present)
                self.assertFalse(r["V5_CLOSED_POSITION_VERIFIED"])

    def test_wrong_or_duplicate_rows_fail_shape(self):
        for bad in ([historical(),historical()],
                    [historical(symbol="ETH_USDT")],
                    ["some row"],
                    [historical()] * 101):
            with self.subTest(rows=str(bad)[:20]):
                r=classify(journal(),bad,bad,[],[])
                self.assertEqual(r["V5_EXACT_CLOSED_POSITION_HISTORY"],
                                 "HISTORY_ROWS_UNVERIFIED")
                self.assertFalse(r["V5_CLOSED_POSITION_VERIFIED"])

    def test_wrong_type_state_quantity_and_direction_block(self):
        cases=[
            {"positionType":1},{"openType":2},{"state":2},
            {"holdVol":1},{"closeVol":"0.5"},
            {"positionShowStatus":"OPEN"},{"positionShowStatus":None},
        ]
        for change in cases:
            with self.subTest(case=change):
                r=classify(journal(),[historical(**change)],
                           [historical(**change)],[],[])
                self.assertFalse(r["V5_CLOSED_POSITION_VERIFIED"])

    def test_different_history_read_is_not_certified(self):
        r=classify(journal(),[historical()],
                   [historical(closeVol="0.5")],[],[])
        self.assertFalse(r["V5_CLOSED_POSITION_VERIFIED"])

    def test_open_filter_wrong_identity_does_not_certify(self):
        r=classify(journal(),[historical()],[historical()],
                   [dict(positionId=999)],[])
        self.assertEqual(r["V5_EXACT_CLOSED_POSITION_HISTORY"],
                         "EXACT_POSITION_FILTER_UNVERIFIED")

    def test_get_policy_only_history_and_exact_position_id(self):
        key="TEST_KEY";secret="TEST_SECRET"
        for path,params in (
            (HISTORY,dict(symbol="BTC_USDT",page_num=1,page_size=100)),
            (OPEN,dict(positionId=555)),
        ):
            o=Opener([])
            out=_get(path=path,params=params,key=key,secret=secret,
                     opener=o,stamp_ms=1762370000000)
            self.assertEqual(out,[])
            self.assertEqual(len(o.calls),1)
            req,_=o.calls[0]
            self.assertEqual(req.get_method(),"GET")
            parts=urlsplit(req.full_url)
            self.assertEqual(parts.netloc,"api.mexc.com")
            self.assertEqual(parts.path,path)
            query=parts.query
            self.assertEqual(parse_qsl(query),sorted((k,str(v)) for k,v in params.items()))
            header=dict(req.header_items())
            self.assertEqual(header["Signature"],
                hmac.new(secret.encode(),
                         f"{key}1762370000000{query}".encode(),
                         hashlib.sha256).hexdigest())
        for bad_path,bad_params in (
            ("/api/v1/private/order/create",dict(positionId=555)),
            (OPEN,dict(positionId=0)),
            (HISTORY,dict(symbol="BTC_USDT",page_num=2,page_size=100)),
        ):
            with self.subTest(path=bad_path,params=bad_params):
                with self.assertRaises(AuditHalt):
                    _get(path=bad_path,params=bad_params,
                         key="key",secret="secret",opener=Opener([]))

    def test_v5_journal_is_read_only_and_does_not_follow_symlinks(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/"journal.json"
            path.write_text(json.dumps(journal()))
            before=path.read_bytes()
            self.assertEqual(_journal(path)["position_id"],"555")
            self.assertEqual(path.read_bytes(),before)
            symlink=Path(tmp)/"link.json"
            symlink.symlink_to(path)
            with self.assertRaises(AuditHalt):
                _journal(symlink)

    def test_malformed_journal_cannot_relabel_manual_position(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/"journal.json"
            for change in ({"entry_oid":"manual"},
                           {"close_oid":"manual"},
                           {"position_id":"0"},
                           {"position_id":"not_numeric"}):
                invalid=dict(journal(),**change)
                path.write_text(json.dumps(invalid))
                with self.subTest(change=change):
                    with self.assertRaises(AuditHalt):
                        _journal(path)


if __name__=="__main__":
    unittest.main()

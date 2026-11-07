"""No-live-order tests for the one-shot local MEXC history schema probe."""
from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import parse_qsl, urlsplit
import unittest

from tools.t100_mexc_history_shape_probe_local import (
    _get_history_first_page, _RejectRedirects, ProbeHalt,
    summarize_shape, BASE, PATH,
)


class FakeReply:
    def __init__(self, response):
        self.payload = response

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size=-1):
        return json.dumps(self.payload).encode("utf-8")


class FakeOpener:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def open(self, req, timeout=12):
        self.calls.append((req,timeout))
        return FakeReply({"success":True,"code":0,"data":self.data})


class HistoryProbeTests(unittest.TestCase):
    def test_only_one_get_on_current_official_host_with_signed_sorted_query(self):
        mock=FakeOpener([dict(positionId=123,state=3)])
        result=_get_history_first_page(
            key="TEST_API_KEY",secret="TEST_API_SECRET_ABCDE",
            opener=mock,stamp_ms=1730000000000,
        )
        self.assertEqual(len(result),1)
        self.assertEqual(len(mock.calls),1)
        req,timeout=mock.calls[0]
        self.assertEqual(req.get_method(),"GET")
        self.assertEqual(urlsplit(req.full_url).netloc,"api.mexc.com")
        self.assertEqual(urlsplit(req.full_url).path,PATH)
        query=urlsplit(req.full_url).query
        self.assertEqual(
            parse_qsl(query),
            [("page_num","1"),("page_size","20"),("symbol","BTC_USDT")],
        )
        header=dict(req.header_items())
        self.assertIn("Apikey",header)
        self.assertIn("Signature",header)
        signed="TEST_API_KEY"+"1730000000000"+query
        expected=hmac.new(b"TEST_API_SECRET_ABCDE",
                          signed.encode(),hashlib.sha256).hexdigest()
        self.assertEqual(header["Signature"],expected)
        self.assertEqual(timeout,12)

    def test_array_shape_never_claims_history_completeness(self):
        result=summarize_shape([
            dict(positionId=123,symbol="BTC_USDT",positionType=2,
                 openType=1,state=3,holdVol=0,closeVol=1,
                 positionShowStatus="CLOSED"),
        ])
        self.assertEqual(result["DATA_SHAPE"],"ARRAY_WITHOUT_PAGINATION_METADATA")
        self.assertFalse(result["PAGINATION_METADATA_TYPES_VALID"])
        self.assertEqual(result["FIRST_PAGE_ROWS"],1)
        self.assertFalse(result["FULL_HISTORY_CERTIFIED"])
        self.assertFalse(result["POSITION_CLOSURE_CERTIFIED"])
        self.assertFalse(result["LIVE_AUTHORIZATION"])
        self.assertEqual(result["EXCHANGE_MUTATIONS"],0)
        self.assertNotIn("123",json.dumps(result))

    def test_object_with_metadata_presence_not_certification(self):
        result=summarize_shape(dict(
            resultList=[dict(positionId=999)],
            pageSize=20,currentPage=1,totalCount=1,totalPage=1,
        ))
        self.assertEqual(result["DATA_SHAPE"],"OBJECT_WITH_RESULT_LIST")
        self.assertTrue(result["PAGINATION_METADATA_TYPES_VALID"])
        self.assertFalse(result["FULL_HISTORY_CERTIFIED"])
        self.assertNotIn("999",json.dumps(result))

    def test_missing_or_partial_response_metadata_does_not_upgrade(self):
        for payload in (None,5,{"other":"value"},
                        {"resultList":[], "totalCount":"1"},
                        [], {}):
            with self.subTest(payload=payload):
                result=summarize_shape(payload)
                self.assertFalse(result["FULL_HISTORY_CERTIFIED"])
                self.assertFalse(result["POSITION_CLOSURE_CERTIFIED"])
                self.assertFalse(result["LIVE_AUTHORIZATION"])

    def test_malformed_rows_block_redaction_boundary(self):
        for payload in ([1], [{"id":2}] * 21,
                        {"resultList":["abc"]}):
            with self.subTest(payload=payload):
                with self.assertRaises(ProbeHalt):
                    summarize_shape(payload)

    def test_mexc_explicit_api_rejection_halts_silently(self):
        class RejectOpener(FakeOpener):
            def open(self,req,timeout=12):
                self.calls.append((req,timeout))
                return FakeReply({
                    "success":False,"code":602,
                    "message":"secret-value-from-exchange",
                })
        mock=RejectOpener(None)
        with self.assertRaisesRegex(ProbeHalt,"REJECTED") as err:
            _get_history_first_page(
                key="SAMPLE_API_KEY",secret="SAMPLE_SECRET",
                opener=mock,stamp_ms=1730000000000,
            )
        self.assertNotIn("secret-value",str(err.exception))
        self.assertEqual(len(mock.calls),1)

    def test_redirects_are_refused(self):
        with self.assertRaises(ProbeHalt):
            _RejectRedirects().redirect_request(
                None,None,302,"redirect",{}, "http://evil.example"
            )


if __name__=="__main__":
    unittest.main()

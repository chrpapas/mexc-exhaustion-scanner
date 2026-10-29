"""Offline replay evidence tests. No MEXC requests, no credentials."""
import copy
import unittest
from app.t100_live_shadow_replay import replay_shadow_records, ReplayHalted


def record(t=1000):
    return {
        "positions": [{"symbol":"SIREN_USDT","positionId":"10",
                       "positionType":2,"holdVol":"20","state":1}],
        "stops": [{"positionId":"10","id":"99","state":1,
                   "stopLossPrice":"175","vol":"20"}],
        "expected": [{"symbol":"SIREN_USDT","position_id":"10",
                      "contracts":"20","entry_price":"100",
                      "stop_order_id":"99","stop_price":"175",
                      "best_profit_pct":"0","armed":False}],
        "observed_at_ms":t,"now_ms":t+500,
        "positions_complete":True,"stops_complete":True,
    }


class ReplayTests(unittest.TestCase):
    def test_two_healthy_records(self):
        result = replay_shadow_records([record(),record(2000)])
        self.assertEqual(result.observations,2)
        self.assertEqual(result.mode,"SHADOW_ONLY")
        self.assertEqual(result.status,"OFFLINE_FIXTURE_REPLAY_ONLY")
        self.assertEqual(len(result.input_sha256),64)

    def test_reproducible_digest(self):
        records=[record(),record(2000)]
        self.assertEqual(replay_shadow_records(records).input_sha256,
                         replay_shadow_records(records).input_sha256)

    def test_changed_payload_changes_digest(self):
        a=record()
        b=copy.deepcopy(a)
        b["stops"][0]["stopLossPrice"]="174"
        self.assertNotEqual(replay_shadow_records([a]).input_sha256,
                            replay_shadow_records([b]).input_sha256)

    def test_trailing_proposal_count(self):
        a=record()
        a["observed_lows"]={"SIREN_USDT":"86"}
        a["price_ticks"]={"SIREN_USDT":"0.01"}
        self.assertEqual(replay_shadow_records([a]).proposals,1)

    def test_flat_shadow_replay(self):
        a=record()
        a["positions"]=[];a["stops"]=[];a["expected"]=[]
        self.assertEqual(replay_shadow_records([a]).observations,1)

    def test_empty_input_blocks(self):
        with self.assertRaisesRegex(ReplayHalted,"EMPTY_SHADOW_REPLAY"):
            replay_shadow_records([])

    def test_duplicate_timestamp_blocks(self):
        with self.assertRaisesRegex(ReplayHalted,"RECORD_2"):
            replay_shadow_records([record(),record()])

    def test_gap_blocks(self):
        with self.assertRaisesRegex(ReplayHalted,"RECORD_2"):
            replay_shadow_records([record(),record(100000)])

    def test_missing_stop_blocks(self):
        a=record();a["stops"]=[]
        with self.assertRaisesRegex(ReplayHalted,"RECORD_1"):
            replay_shadow_records([a])

    def test_incomplete_page_blocks(self):
        a=record();a["stops_complete"]=False
        with self.assertRaisesRegex(ReplayHalted,"RECORD_1"):
            replay_shadow_records([a])

    def test_unknown_field_blocks(self):
        a=record();a["api_secret"]="NEVER_INCLUDE"
        with self.assertRaisesRegex(ReplayHalted,"RECORD_1"):
            replay_shadow_records([a])

    def test_missing_field_blocks(self):
        a=record();del a["positions"]
        with self.assertRaisesRegex(ReplayHalted,"RECORD_1"):
            replay_shadow_records([a])

    def test_nan_blocks(self):
        a=record();a["expected"][0]["contracts"]="NaN"
        with self.assertRaisesRegex(ReplayHalted,"RECORD_1"):
            replay_shadow_records([a])

    def test_nonboolean_complete_blocks(self):
        a=record();a["positions_complete"]=1
        with self.assertRaisesRegex(ReplayHalted,"RECORD_1"):
            replay_shadow_records([a])

    def test_nonboolean_armed_blocks(self):
        a=record();a["expected"][0]["armed"]=1
        with self.assertRaisesRegex(ReplayHalted,"RECORD_1"):
            replay_shadow_records([a])

    def test_record_limit_blocks(self):
        with self.assertRaisesRegex(ReplayHalted,"RECORD_2"):
            replay_shadow_records([record(),record(2000)],max_records=1)

    def test_invalid_limit_blocks(self):
        with self.assertRaises(ValueError):
            replay_shadow_records([record()],max_records=0)

    def test_numeric_market_data_must_be_string(self):
        a=record();a["observed_lows"]={"SIREN_USDT":86}
        a["price_ticks"]={"SIREN_USDT":"0.01"}
        with self.assertRaisesRegex(ReplayHalted,"RECORD_1"):
            replay_shadow_records([a])

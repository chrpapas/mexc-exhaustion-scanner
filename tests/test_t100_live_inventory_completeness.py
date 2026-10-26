"""Offline current MEXC inventory completeness certificate tests."""
import unittest
from app.t100_live_inventory_completeness import (
    CurrentInventoryContractEvidence, assess_current_inventory_completeness,
)

def margin(ok=True):
    return {"position_inventory_margin_witness_observed": ok}
def cross(ok=True):
    return {"symbol_scoped_crosscheck_verified": ok}
def pos():
    return {"positionId": "10", "symbol": "SIREN_USDT"}
def stop():
    return {"id": "99", "positionId": "10", "symbol": "SIREN_USDT"}

class InventoryCompletenessTests(unittest.TestCase):
    def test_nonflat_bundle_verifies_current_inventory(self):
        r=assess_current_inventory_completeness(
            positions=[pos()],stops=[stop()],
            open_order_counts={"stopOrderCount":1},
            critical_inventory_double_read_consistent=True,
            position_margin_witness=margin(),
            symbol_scoped_crosscheck=cross())
        self.assertTrue(r["account_wide_completeness_verified"])
        self.assertFalse(r["exchange_atomicity_verified"])
        self.assertFalse(r["live_authorization"])
    def test_flat_bundle_can_verify_without_symbol_crosscheck(self):
        r=assess_current_inventory_completeness(
            positions=[],stops=[],open_order_counts={"stopOrderCount":0},
            critical_inventory_double_read_consistent=True,
            position_margin_witness=margin(),symbol_scoped_crosscheck={})
        self.assertTrue(r["account_wide_completeness_verified"])
        self.assertFalse(r["symbol_scoped_redundancy_required"])
    def test_stop_count_mismatch_blocks(self):
        r=assess_current_inventory_completeness(
            positions=[pos()],stops=[stop()],
            open_order_counts={"stopOrderCount":2},
            critical_inventory_double_read_consistent=True,
            position_margin_witness=margin(),symbol_scoped_crosscheck=cross())
        self.assertFalse(r["account_wide_completeness_verified"])
        self.assertEqual(r["status"],"CURRENT_TPSL_COUNT_MISMATCH")
    def test_margin_witness_blocks(self):
        r=assess_current_inventory_completeness(
            positions=[pos()],stops=[stop()],
            open_order_counts={"stopOrderCount":1},
            critical_inventory_double_read_consistent=True,
            position_margin_witness=margin(False),symbol_scoped_crosscheck=cross())
        self.assertFalse(r["current_position_inventory_completeness_verified"])
    def test_nonflat_symbol_mismatch_blocks(self):
        r=assess_current_inventory_completeness(
            positions=[pos()],stops=[stop()],
            open_order_counts={"stopOrderCount":1},
            critical_inventory_double_read_consistent=True,
            position_margin_witness=margin(),symbol_scoped_crosscheck=cross(False))
        self.assertFalse(r["account_wide_completeness_verified"])
    def test_critical_drift_blocks(self):
        r=assess_current_inventory_completeness(
            positions=[pos()],stops=[stop()],
            open_order_counts={"stopOrderCount":1},
            critical_inventory_double_read_consistent=False,
            position_margin_witness=margin(),symbol_scoped_crosscheck=cross())
        self.assertEqual(r["status"],"CURRENT_INVENTORY_NOT_STABLE")
    def test_invalid_count_fails_closed(self):
        for value in (None,-1,True,1.0,"1"):
            r=assess_current_inventory_completeness(
                positions=[],stops=[],open_order_counts={"stopOrderCount":value},
                critical_inventory_double_read_consistent=True,
                position_margin_witness=margin(),symbol_scoped_crosscheck={})
            self.assertFalse(r["account_wide_completeness_verified"])
    def test_unreviewed_contract_blocks(self):
        contract=CurrentInventoryContractEvidence("test",False,True,True)
        r=assess_current_inventory_completeness(
            positions=[pos()],stops=[stop()],
            open_order_counts={"stopOrderCount":1},
            critical_inventory_double_read_consistent=True,
            position_margin_witness=margin(),symbol_scoped_crosscheck=cross(),
            contract=contract)
        self.assertFalse(r["account_wide_completeness_verified"])
    def test_output_redacts_inventory_values(self):
        r=assess_current_inventory_completeness(
            positions=[{"positionId":"PRIVATE-123","symbol":"SECRET_USDT"}],
            stops=[{"id":"PRIVATE-STOP","positionId":"PRIVATE-123","symbol":"SECRET_USDT"}],
            open_order_counts={"stopOrderCount":1},
            critical_inventory_double_read_consistent=True,
            position_margin_witness=margin(),symbol_scoped_crosscheck=cross())
        rendered=str(r)
        for secret in ("PRIVATE-123","PRIVATE-STOP","SECRET_USDT"):
            self.assertNotIn(secret,rendered)
if __name__=="__main__":
    unittest.main()

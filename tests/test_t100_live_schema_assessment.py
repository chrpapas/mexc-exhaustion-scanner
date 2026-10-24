"""Offline tests for redacted venue schema compatibility assessment."""
import unittest
from app.t100_live_contract_evidence import summarize_inventory_schema
from app.t100_live_schema_assessment import assess_shadow_schema


def pos(**extra):
    return dict(symbol="SIREN_USDT", positionId="10", positionType=2,
                holdVol="20", **extra)


def stop(**extra):
    return dict(id="99", positionId="10", state=1,
                stopLossPrice="175", vol="20", **extra)


class SchemaAssessmentTests(unittest.TestCase):
    def test_empty_account_does_not_certify_schema(self):
        r = assess_shadow_schema(summarize_inventory_schema([], []))
        self.assertEqual(r["overall"], "INCOMPLETE_SCHEMA_EVIDENCE")
        self.assertFalse(r["shadow_parser_contract_verified"])

    def test_expected_shapes_still_uncertified(self):
        r = assess_shadow_schema(summarize_inventory_schema([pos()], [stop()]))
        self.assertEqual(r["overall"], "PARSER_SHAPE_COMPATIBLE_UNCERTIFIED")
        self.assertFalse(r["venue_semantics_verified"])
        self.assertFalse(r["pagination_verified"])

    def test_missing_position_type_flagged(self):
        p = pos()
        del p["positionType"]
        r = assess_shadow_schema(summarize_inventory_schema([p], [stop()]))
        self.assertIn("positionType", r["families"]["positions"]["missing_fields"])
        self.assertEqual(r["overall"], "PARSER_SHAPE_MISMATCH")

    def test_missing_stop_volume_flagged(self):
        s = stop()
        del s["vol"]
        r = assess_shadow_schema(summarize_inventory_schema([pos()], [s]))
        self.assertIn("vol", r["families"]["stops"]["missing_fields"])

    def test_nullable_stop_price_flagged(self):
        s = stop()
        s["stopLossPrice"] = None
        r = assess_shadow_schema(summarize_inventory_schema([pos()], [s]))
        self.assertIn("stopLossPrice", r["families"]["stops"]["incompatible_fields"])

    def test_numeric_stop_price_is_compatible_shape_only(self):
        s = stop()
        s["stopLossPrice"] = 175.0
        r = assess_shadow_schema(summarize_inventory_schema([pos()], [s]))
        self.assertEqual(r["overall"], "PARSER_SHAPE_COMPATIBLE_UNCERTIFIED")

    def test_alternative_position_identity(self):
        p = pos()
        p["id"] = p.pop("positionId")
        r = assess_shadow_schema(summarize_inventory_schema([p], [stop()]))
        self.assertEqual(r["overall"], "PARSER_SHAPE_COMPATIBLE_UNCERTIFIED")

    def test_alternative_stop_identity(self):
        s = stop()
        s["stopPlanOrderId"] = s.pop("id")
        r = assess_shadow_schema(summarize_inventory_schema([pos()], [s]))
        self.assertEqual(r["overall"], "PARSER_SHAPE_COMPATIBLE_UNCERTIFIED")

    def test_missing_all_position_id_alternatives(self):
        p = pos()
        del p["positionId"]
        r = assess_shadow_schema(summarize_inventory_schema([p], [stop()]))
        self.assertTrue(any(k.startswith("ONE_OF_")
                            for k in r["families"]["positions"]["missing_fields"]))

    def test_missing_all_stop_id_alternatives(self):
        s = stop()
        del s["id"]
        r = assess_shadow_schema(summarize_inventory_schema([pos()], [s]))
        self.assertTrue(any(k.startswith("ONE_OF_")
                            for k in r["families"]["stops"]["missing_fields"]))

    def test_mixed_position_type_detects_null(self):
        p = pos()
        p["positionType"] = None
        r = assess_shadow_schema(summarize_inventory_schema([pos(), p], [stop()]))
        self.assertIn("positionType", r["families"]["positions"]["incompatible_fields"])

    def test_invalid_evidence_fails_closed(self):
        self.assertEqual(assess_shadow_schema(None)["overall"], "INVALID_EVIDENCE")
        self.assertEqual(assess_shadow_schema({})["overall"], "INVALID_EVIDENCE")

    def test_schema_evidence_does_not_echo_values(self):
        r = assess_shadow_schema(summarize_inventory_schema(
            [pos()], [stop()]))
        self.assertNotIn("SIREN_USDT", str(r))
        self.assertNotIn("175", str(r))

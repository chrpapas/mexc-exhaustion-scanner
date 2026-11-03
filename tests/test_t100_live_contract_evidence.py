"""Pure schema-only evidence tests; never output account values."""
import unittest

from app.t100_live_contract_evidence import (
    ContractEvidenceHalted, summarize_inventory_schema, summarize_rows,
)


class ContractEvidenceTests(unittest.TestCase):
    def test_empty_is_not_contract_evidence(self):
        report = summarize_inventory_schema([], [])
        self.assertTrue(report["positions"]["empty_rows_cannot_establish_schema"])
        self.assertFalse(report["stops"]["contract_verified"])

    def test_allowlisted_types_without_values(self):
        report = summarize_rows(
            [{"positionId": "private-123", "symbol": "SIREN_USDT",
              "positionType": 2, "holdVol": "20"}], family="positions")
        self.assertEqual(report["allowlisted_field_types"]["positionId"], ["string"])
        self.assertEqual(report["allowlisted_field_types"]["positionType"], ["integer"])
        self.assertNotIn("private-123", str(report))
        self.assertNotIn("SIREN_USDT", str(report))

    def test_unknown_field_names_and_values_not_leaked(self):
        report = summarize_rows([{"SECRET_KEY_ABC": "DO_NOT_EXPOSE"}], family="stops")
        self.assertEqual(report["unlisted_field_occurrences"], 1)
        self.assertNotIn("SECRET_KEY_ABC", str(report))
        self.assertNotIn("DO_NOT_EXPOSE", str(report))

    def test_mixed_types_are_visible(self):
        report = summarize_rows([{"holdVol": "20"}, {"holdVol": 20}], family="positions")
        self.assertEqual(report["allowlisted_field_types"]["holdVol"],
                         ["integer", "string"])

    def test_null_field_type_is_visible(self):
        report = summarize_rows([{"stopLossPrice": None}], family="stops")
        self.assertEqual(report["allowlisted_field_types"]["stopLossPrice"], ["null"])

    def test_invalid_family_rejected(self):
        with self.assertRaisesRegex(ContractEvidenceHalted, "UNKNOWN_SCHEMA_FAMILY"):
            summarize_rows([], family="orders")

    def test_non_list_rejected(self):
        with self.assertRaisesRegex(ContractEvidenceHalted, "MALFORMED_SCHEMA_ROWS"):
            summarize_rows({}, family="positions")

    def test_non_dict_row_rejected(self):
        with self.assertRaisesRegex(ContractEvidenceHalted, "MALFORMED_SCHEMA_ROWS"):
            summarize_rows([{}, None], family="positions")

    def test_nonstring_key_rejected(self):
        with self.assertRaisesRegex(ContractEvidenceHalted, "NONSTRING_FIELD_NAME"):
            summarize_rows([{1: "bad"}], family="positions")

    def test_no_account_values_in_combined_report(self):
        r = summarize_inventory_schema(
            [{"positionId": "PRIVATE-ID", "symbol": "TOKEN_USDT"}],
            [{"id": "STOP-SECRET", "stopLossPrice": "17.19"}],
        )
        for value in ("PRIVATE-ID", "TOKEN_USDT", "STOP-SECRET", "17.19"):
            self.assertNotIn(value, str(r))
        self.assertEqual(r["note"],
                         "REDACTED_SHAPE_ONLY_NOT_ATOMICITY_OR_PAGINATION_EVIDENCE")

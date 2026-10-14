import io
import json
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError
from overlay.oracle_fccs import OracleFCCS, OracleError, HOST, NoRedirect


class LiveAdapterTests(unittest.TestCase):
    def setUp(self):
        self.env = {"EPM_ENTRA_USER": "reader@example.com", "EPM_USERNAME": "oracle-reader",
                    "EPM_PASSWORD": "test-only", "EPM_CUBE": "Consol"}
        self.opener = Mock()
        self.adapter = OracleFCCS(self.env, self.opener)

    def respond(self, *payloads):
        self.opener.open.side_effect = [io.BytesIO(json.dumps(p).encode()) for p in payloads]

    def test_unbound_identity_never_calls_oracle(self):
        with self.assertRaises(OracleError):
            self.adapter.dimensions("different@example.com")
        self.opener.open.assert_not_called()

    def test_missing_credentials_never_calls_oracle(self):
        del self.env["EPM_PASSWORD"]
        with self.assertRaises(OracleError):
            self.adapter.dimensions("reader@example.com")
        self.opener.open.assert_not_called()

    def test_unknown_host_never_receives_credentials(self):
        self.env["EPM_BASE_URL"] = "https://example.com"
        with self.assertRaises(OracleError):
            self.adapter.dimensions("reader@example.com")
        self.opener.open.assert_not_called()

    def test_complete_grid_keeps_missing_and_parent_values(self):
        dims = ["Account", "Entity", "Period", "Year", "Scenario", "View", "Currency"]
        output = {"columns": [["Revenue"]], "rows": [{"headers": ["Parent", "Sep"], "data": [None]}]}
        self.respond({"items": [{"name": d} for d in dims]}, output)
        result = self.adapter.financials("reader@example.com", ["Revenue"], "Sep", "YTD", "Actual",
                                         ["Parent"], "FY27", {"Currency": "GBP"})
        self.assertEqual(result["grid"], output)
        self.assertNotIn("currency", result)
        req = self.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, HOST + "/HyperionPlanning/rest/v3/applications/NICSFCC/plantypes/Consol/exportdataslice")
        grid = json.loads(req.data)["gridDefinition"]
        self.assertEqual(grid["pov"]["members"], [["FY27"], ["Actual"], ["FCCS_YTD"], ["GBP"]])
        self.assertEqual(grid["rows"][0]["members"], [["Parent"], ["Sep"]])

    def test_incomplete_pov_blocks_data_export(self):
        self.respond({"items": [{"name": d} for d in ["Account", "Entity", "Period", "Year", "Scenario", "View", "Movement"]]})
        with self.assertRaisesRegex(OracleError, "Missing: Movement"):
            self.adapter.financials("reader@example.com", ["Revenue"], "Sep", "YTD", "Actual", ["Parent"], "FY27", {})
        self.assertEqual(self.opener.open.call_count, 1)

    def test_member_expression_is_rejected(self):
        with self.assertRaises(OracleError):
            self.adapter.financials("reader@example.com", ["Revenue"], "Sep", "YTD", "Actual",
                                     ["Parent"], "FY27", {"Movement": "@RELATIVE(Movement,0)"})
        self.opener.open.assert_not_called()

    def test_oracle_error_body_is_not_disclosed(self):
        self.opener.open.side_effect = HTTPError(HOST, 401, "sensitive detail", {}, None)
        with self.assertRaisesRegex(OracleError, "HTTP 401") as caught:
            self.adapter.dimensions("reader@example.com")
        self.assertNotIn("sensitive detail", str(caught.exception))

    def test_redirects_cannot_forward_authentication(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))


if __name__ == "__main__":
    unittest.main()

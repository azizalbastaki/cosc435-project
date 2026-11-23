import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from security_demo import main, run_scenarios


class SecurityDemoTests(unittest.TestCase):
    def test_control_and_three_security_scenarios(self):
        results = run_scenarios()
        self.assertEqual(len(results), 4)
        self.assertTrue(all(result["passed"] for result in results))
        self.assertEqual([result["decision"] for result in results],
                         ["APPROVE", "BLOCK", "BLOCK", "BLOCK"])

    def test_failed_check_returns_nonzero(self):
        failed = {"scenario": "synthetic failure", "passed": False,
                  "decision": "APPROVE", "reason": "Unexpected approval."}
        with patch("security_demo.run_scenarios", return_value=[failed]), patch("builtins.print"):
            self.assertEqual(main([]), 1)

    def test_export_records_setup_and_results_without_claiming_live_activity(self):
        with tempfile.TemporaryDirectory() as directory, patch("builtins.print"):
            path = Path(directory) / "evidence.json"
            self.assertEqual(main(["--output", str(path)]), 0)
            report = json.loads(path.read_text())
            self.assertEqual(report["mode"], "offline_scripted_models")
            self.assertEqual(report["ledger_requests"], 0)
            for row in report["results"]:
                self.assertIsNone(row["transaction_id"])
                self.assertFalse(row["server_backstop_tested"])
                self.assertEqual(row["decision"], row["expected_decision"])
                self.assertEqual(row["model_calls"], row["expected_model_calls"])
                self.assertNotIn("transaction_id", row["setup"]["order"])
            self.assertEqual(report["results"][3]["setup"]["during_review"],
                             {"max_transaction_amount": 100})
            original = path.read_bytes()
            self.assertEqual(main(["--output", str(path)]), 1)
            self.assertEqual(path.read_bytes(), original)

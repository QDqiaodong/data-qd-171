import json
import unittest
from pathlib import Path
from baggage.core import reconcile


class ReconcileTests(unittest.TestCase):
    def test_fixture_reconciles_latest_scan_and_connection(self):
        data = json.loads(Path("fixtures/input.json").read_text())
        result = reconcile(data)
        self.assertEqual(result["summary"], {"total": 3, "not_seen": 0, "in_transit": 1, "arrived": 2, "connection_risk": 1})
        self.assertEqual(result["bags"][0]["last_scan"], "S1")

    def test_rejects_unknown_bag_scan(self):
        with self.assertRaisesRegex(ValueError, "unknown bag"):
            reconcile({"flights": [], "bags": [], "scans": [{"id": "s", "bag_id": "x", "station": "a", "event_time": "2026-01-01T00:00:00+00:00"}]})


if __name__ == "__main__":
    unittest.main()

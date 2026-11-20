import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"


class ReadAuditLoadTool(unittest.TestCase):
    def test_generator_emits_required_scale_and_one_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, str(TOOLS / "read_audit_load.py"),
                 "--out", tmp, "--bags", "120", "--reads", "120",
                 "--candidates", "960", "--component-share", "0.9"],
                check=True, capture_output=True, text=True,
            )
            info = json.loads(result.stdout)
            self.assertEqual(info["bags"], 120)
            self.assertEqual(info["reads"], 120)
            self.assertEqual(info["candidate_refs"], 960)

            batch = json.loads((Path(tmp) / "batch.json").read_text())
            self.assertEqual(len(batch["reads"]), 120)
            # Most candidates sit in one connected component.
            parent = {f"B{i:05d}": f"B{i:05d}" for i in range(120)}

            def find(node):
                while parent[node] != node:
                    parent[node] = parent[parent[node]]
                    node = parent[node]
                return node

            def union(a, b):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb

            for read in batch["reads"]:
                ids = read["candidate_bag_ids"]
                for other in ids[1:]:
                    union(ids[0], other)
            groups = {find(bag) for bag in parent}
            self.assertEqual(len(groups), 1)

            decisions = json.loads((Path(tmp) / "decisions.json").read_text())
            self.assertEqual(len(decisions["confirm"]), 1)
            self.assertEqual(len(decisions["discard"]), 1)


class ReadAuditBenchTool(unittest.TestCase):
    def test_benchmark_reports_timing_and_memory_without_dropping_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, str(TOOLS / "read_audit_bench.py"),
                 "--work", tmp, "--bags", "200", "--reads", "200",
                 "--candidates", "1600"],
                check=True, capture_output=True, text=True,
            )
            data = json.loads(result.stdout)
            self.assertGreater(data["timing_seconds"], 0)
            self.assertGreater(data["peak_rss_kib"], 0)
            self.assertEqual(data["result"]["scoped_bag_count"], 200)
            # Every bag and read has a conclusion; nothing is omitted.
            self.assertEqual(data["result"]["report_bag_rows"], 200)
            self.assertEqual(data["result"]["report_read_rows"], 200)


if __name__ == "__main__":
    unittest.main()

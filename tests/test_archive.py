import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from baggage import archive
from baggage.cli import main

INITIAL = {
    "flights": [
        {"id": "F1", "origin": "AAA", "destination": "BBB",
         "departure": "2026-01-01T10:00:00+00:00", "arrival": "2026-01-01T11:00:00+00:00"},
        {"id": "F2", "origin": "BBB", "destination": "CCC",
         "departure": "2026-01-01T11:40:00+00:00", "arrival": "2026-01-01T12:30:00+00:00"},
    ],
    "bags": [
        {"id": "B", "flight_id": "F1", "next_flight_id": "F2",
         "minimum_connection_minutes": 30},
    ],
    "scans": [
        {"id": "S1", "bag_id": "B", "station": "BBB",
         "event_time": "2026-01-01T10:55:00+00:00"},
    ],
}

SCAN_CORRECTION = {
    "batch_id": "BATCH-1",
    "scans": [
        {"op": "replace", "scan_id": "S1",
         "scan": {"id": "S1", "bag_id": "B", "station": "BBB",
                  "event_time": "2026-01-01T11:20:00+00:00"}},
    ],
}

FLIGHT_CORRECTION = {
    "batch_id": "BATCH-2",
    "flights": [
        {"flight_id": "F2", "origin": "BBB", "destination": "CCC",
         "departure": "2026-01-01T12:10:00+00:00",
         "arrival": "2026-01-01T12:30:00+00:00"},
    ],
}


class ArchiveCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.input_path = self.root / "input.json"
        self.input_path.write_text(json.dumps(INITIAL), encoding="utf-8")
        self.archive_path = self.root / "archive"

    def write_batch(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def init(self):
        seq = archive.init_archive(self.input_path, self.archive_path)
        self.assertEqual(seq, 1)
        self.assertTrue((self.archive_path / archive.EVENTS_FILE).is_file())
        return self.archive_path

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            main(list(argv))
        return out.getvalue().strip(), err.getvalue().strip()

    # ---- acceptance scenario -------------------------------------------------

    def test_acceptance_scenario_scan_then_flight_corrections(self):
        self.init()
        report_1 = archive.build_report(self.archive_path, 1)
        self.assertFalse(report_1["bags"][0]["connection_at_risk"])
        self.assertEqual(report_1["knowledge_seq"], 1)
        self.assertEqual(report_1["sources"]["scans"][0],
                         {"scan_id": "S1", "knowledge_seq": 1, "batch_id": None})
        self.assertEqual(report_1["sources"]["flights"][1],
                         {"flight_id": "F2", "knowledge_seq": 1, "batch_id": None})

        seq, _ = archive.apply_batch(self.archive_path, self.write_batch("b1.json", SCAN_CORRECTION))
        self.assertEqual(seq, 2)
        report_2 = archive.build_report(self.archive_path, 2)
        self.assertTrue(report_2["bags"][0]["connection_at_risk"])
        self.assertEqual(report_2["sources"]["scans"][0],
                         {"scan_id": "S1", "knowledge_seq": 2, "batch_id": "BATCH-1"})
        self.assertEqual(report_2["sources"]["flights"][1],
                         {"flight_id": "F2", "knowledge_seq": 1, "batch_id": None})

        seq, _ = archive.apply_batch(self.archive_path, self.write_batch("b2.json", FLIGHT_CORRECTION))
        self.assertEqual(seq, 3)
        report_3 = archive.build_report(self.archive_path, 3)
        self.assertFalse(report_3["bags"][0]["connection_at_risk"])
        self.assertEqual(report_3["sources"]["flights"][1],
                         {"flight_id": "F2", "knowledge_seq": 3, "batch_id": "BATCH-2"})

        # Old sequences stay reproducible and are bound to their own versions.
        again_1 = archive.build_report(self.archive_path, 1)
        again_2 = archive.build_report(self.archive_path, 2)
        self.assertEqual(again_1, report_1)
        self.assertEqual(again_2, report_2)
        self.assertFalse(again_1["bags"][0]["connection_at_risk"])
        self.assertTrue(again_2["bags"][0]["connection_at_risk"])

    # ---- initialization ------------------------------------------------------

    def test_init_refuses_existing_archive(self):
        self.init()
        with self.assertRaisesRegex(ValueError, "already exists"):
            archive.init_archive(self.input_path, self.archive_path)

    def test_init_rejects_naive_timestamp_and_bad_flight_times(self):
        bad = json.loads(json.dumps(INITIAL))
        bad["scans"][0]["event_time"] = "2026-01-01T10:55:00"
        self.input_path.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "UTC offset"):
            archive.init_archive(self.input_path, self.archive_path)
        self.assertFalse(self.archive_path.exists())

        bad = json.loads(json.dumps(INITIAL))
        bad["flights"][0]["arrival"] = "2026-01-01T09:59:00+00:00"
        self.input_path.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "arrival must not precede"):
            archive.init_archive(self.input_path, self.archive_path)

    def test_init_rejects_duplicate_and_unknown_references(self):
        bad = json.loads(json.dumps(INITIAL))
        bad["scans"].append(dict(bad["scans"][0]))
        self.input_path.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "scan IDs must be unique"):
            archive.init_archive(self.input_path, self.archive_path)

        bad = json.loads(json.dumps(INITIAL))
        bad["scans"][0]["bag_id"] = "NOPE"
        self.input_path.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unknown bag"):
            archive.init_archive(self.input_path, self.archive_path)

    # ---- batch sequencing and operations ------------------------------------

    def test_append_replace_retract_lifecycle(self):
        self.init()
        append_batch = {"batch_id": "A1", "scans": [
            {"op": "append", "scan": {"id": "S2", "bag_id": "B", "station": "AAA",
                                      "event_time": "2026-01-01T08:00:00+00:00"}}]}
        seq, _ = archive.apply_batch(self.archive_path, self.write_batch("a1.json", append_batch))
        self.assertEqual(seq, 2)
        report = archive.build_report(self.archive_path, 2)
        self.assertEqual({s["scan_id"] for s in report["sources"]["scans"]}, {"S1", "S2"})

        retract_batch = {"batch_id": "A2", "scans": [{"op": "retract", "scan_id": "S2"}]}
        seq, _ = archive.apply_batch(self.archive_path, self.write_batch("a2.json", retract_batch))
        self.assertEqual(seq, 3)
        report = archive.build_report(self.archive_path, 3)
        self.assertEqual({s["scan_id"] for s in report["sources"]["scans"]}, {"S1"})
        self.assertEqual(report["bags"][0]["last_scan"], "S1")

        # The retraction cannot rewrite the earlier sequence: seq 2 still sees S2.
        old_report = archive.build_report(self.archive_path, 2)
        self.assertEqual({s["scan_id"] for s in old_report["sources"]["scans"]}, {"S1", "S2"})
        s2_source = next(s for s in old_report["sources"]["scans"] if s["scan_id"] == "S2")
        self.assertEqual(s2_source, {"scan_id": "S2", "knowledge_seq": 2, "batch_id": "A1"})

        # A retracted id can never be reused by an append.
        reuse = {"batch_id": "A3", "scans": [
            {"op": "append", "scan": {"id": "S2", "bag_id": "B", "station": "AAA",
                                      "event_time": "2026-01-01T08:01:00+00:00"}}]}
        with self.assertRaisesRegex(ValueError, "already been used"):
            archive.apply_batch(self.archive_path, self.write_batch("a3.json", reuse))
        self.assertEqual(archive._load_events(self.archive_path)[-1]["knowledge_seq"], 3)

        # Retracting an inactive id is rejected.
        retract_again = {"batch_id": "A4", "scans": [{"op": "retract", "scan_id": "S2"}]}
        with self.assertRaisesRegex(ValueError, "not active"):
            archive.apply_batch(self.archive_path, self.write_batch("a4.json", retract_again))
        self.assertEqual(archive._load_events(self.archive_path)[-1]["knowledge_seq"], 3)

    def test_replace_must_keep_scan_id_and_target_active(self):
        self.init()
        bad = {"batch_id": "X1", "scans": [
            {"op": "replace", "scan_id": "S1",
             "scan": {"id": "OTHER", "bag_id": "B", "station": "BBB",
                      "event_time": "2026-01-01T11:20:00+00:00"}}]}
        with self.assertRaisesRegex(ValueError, "must keep scan_id"):
            archive.apply_batch(self.archive_path, self.write_batch("x1.json", bad))

        bad = {"batch_id": "X2", "scans": [
            {"op": "replace", "scan_id": "GONE",
             "scan": {"id": "GONE", "bag_id": "B", "station": "BBB",
                      "event_time": "2026-01-01T11:20:00+00:00"}}]}
        with self.assertRaisesRegex(ValueError, "not active"):
            archive.apply_batch(self.archive_path, self.write_batch("x2.json", bad))

    def test_flight_revision_and_bad_revision_rejected(self):
        self.init()
        bad = {"batch_id": "F-BAD", "flights": [
            {"flight_id": "F2", "origin": "BBB", "destination": "CCC",
             "departure": "2026-01-01T12:10:00+00:00",
             "arrival": "2026-01-01T11:00:00+00:00"}]}
        with self.assertRaisesRegex(ValueError, "arrival must not precede"):
            archive.apply_batch(self.archive_path, self.write_batch("fbad.json", bad))

        bad = {"batch_id": "F-UNKNOWN", "flights": [
            {"flight_id": "F9", "origin": "BBB", "destination": "CCC",
             "departure": "2026-01-01T12:10:00+00:00",
             "arrival": "2026-01-01T12:30:00+00:00"}]}
        with self.assertRaisesRegex(ValueError, "not known"):
            archive.apply_batch(self.archive_path, self.write_batch("funknown.json", bad))

        seq, _ = archive.apply_batch(self.archive_path, self.write_batch("fok.json", FLIGHT_CORRECTION))
        self.assertEqual(seq, 2)
        report = archive.build_report(self.archive_path, 2)
        source = next(s for s in report["sources"]["flights"] if s["flight_id"] == "F2")
        self.assertEqual(source, {"flight_id": "F2", "knowledge_seq": 2, "batch_id": "BATCH-2"})

    def test_duplicate_id_within_batch_rejected(self):
        self.init()
        bad = {"batch_id": "D1", "scans": [
            {"op": "retract", "scan_id": "S1"},
            {"op": "replace", "scan_id": "S1",
             "scan": {"id": "S1", "bag_id": "B", "station": "BBB",
                      "event_time": "2026-01-01T11:20:00+00:00"}}]}
        with self.assertRaisesRegex(ValueError, "more than once"):
            archive.apply_batch(self.archive_path, self.write_batch("d1.json", bad))

        bad = {"batch_id": "D2", "flights": [FLIGHT_CORRECTION["flights"][0],
                                             FLIGHT_CORRECTION["flights"][0]]}
        with self.assertRaisesRegex(ValueError, "more than once"):
            archive.apply_batch(self.archive_path, self.write_batch("d2.json", bad))

    def test_empty_batch_and_missing_batch_id_rejected(self):
        self.init()
        for bad in ({"batch_id": "E1"},
                    {"batch_id": "E2", "scans": [], "flights": []},
                    {"scans": []}):
            with self.assertRaises(ValueError):
                archive.apply_batch(self.archive_path, self.write_batch("e.json", bad))
        self.assertEqual(archive._load_events(self.archive_path)[-1]["knowledge_seq"], 1)

    # ---- atomicity -----------------------------------------------------------

    def test_mixed_valid_scan_and_invalid_flight_batch_is_atomic(self):
        self.init()
        log_before = (self.archive_path / archive.EVENTS_FILE).read_bytes()
        mixed = {
            "batch_id": "MIXED",
            "scans": SCAN_CORRECTION["scans"],
            "flights": [
                {"flight_id": "F2", "origin": "BBB", "destination": "CCC",
                 "departure": "2026-01-01T12:10:00",
                 "arrival": "2026-01-01T12:30:00+00:00"},
            ],
        }
        with self.assertRaisesRegex(ValueError, "UTC offset"):
            archive.apply_batch(self.archive_path, self.write_batch("mixed.json", mixed))
        self.assertEqual((self.archive_path / archive.EVENTS_FILE).read_bytes(), log_before)
        self.assertEqual(archive._load_events(self.archive_path)[-1]["knowledge_seq"], 1)
        report = archive.build_report(self.archive_path, 1)
        self.assertFalse(report["bags"][0]["connection_at_risk"])

    def test_duplicate_batch_id_rejected_even_with_same_payload(self):
        self.init()
        path = self.write_batch("b1.json", SCAN_CORRECTION)
        archive.apply_batch(self.archive_path, path)
        with self.assertRaisesRegex(ValueError, "duplicate batch_id"):
            archive.apply_batch(self.archive_path, path)
        self.assertEqual(archive._load_events(self.archive_path)[-1]["knowledge_seq"], 2)

    def test_failed_batch_consumes_no_sequence(self):
        self.init()
        bad = {"batch_id": "FAIL", "scans": [{"op": "append",
              "scan": {"id": "S1", "bag_id": "B", "station": "AAA",
                       "event_time": "2026-01-01T08:00:00+00:00"}}]}
        with self.assertRaisesRegex(ValueError, "already been used"):
            archive.apply_batch(self.archive_path, self.write_batch("fail.json", bad))
        good = self.write_batch("good.json", SCAN_CORRECTION)
        seq, _ = archive.apply_batch(self.archive_path, good)
        self.assertEqual(seq, 2)

    # ---- reporting -----------------------------------------------------------

    def test_unknown_sequence_rejected_without_output_file(self):
        self.init()
        output = self.root / "nope.json"
        for bad_seq in (0, 2, -1):
            with self.assertRaisesRegex(ValueError, "unknown or unaccepted"):
                archive.write_report(self.archive_path, bad_seq, output)
            self.assertFalse(output.exists())

    def test_report_output_is_never_overwritten(self):
        self.init()
        output = self.root / "report.json"
        archive.write_report(self.archive_path, 1, output)
        first = output.read_bytes()
        with self.assertRaises(FileExistsError):
            archive.write_report(self.archive_path, 1, output)
        self.assertEqual(output.read_bytes(), first)

    def test_report_determinism_same_sources_same_business_result(self):
        self.init()
        archive.apply_batch(self.archive_path, self.write_batch("b1.json", SCAN_CORRECTION))
        archive.apply_batch(self.archive_path, self.write_batch("b2.json", FLIGHT_CORRECTION))
        r2a = archive.build_report(self.archive_path, 2)
        r2b = archive.build_report(self.archive_path, 2)
        self.assertEqual(r2a, r2b)
        text_a = json.dumps(r2a, sort_keys=True)
        text_b = json.dumps(r2b, sort_keys=True)
        self.assertEqual(text_a, text_b)

    def test_scan_and_flight_changes_in_one_batch_share_one_sequence(self):
        self.init()
        combined = {
            "batch_id": "COMBINED",
            "scans": SCAN_CORRECTION["scans"],
            "flights": FLIGHT_CORRECTION["flights"],
        }
        seq, _ = archive.apply_batch(self.archive_path, self.write_batch("c.json", combined))
        self.assertEqual(seq, 2)
        report = archive.build_report(self.archive_path, 2)
        # Both source kinds bind to the same sequence and batch.
        self.assertEqual(report["sources"]["scans"][0],
                         {"scan_id": "S1", "knowledge_seq": 2, "batch_id": "COMBINED"})
        self.assertEqual(report["sources"]["flights"][1],
                         {"flight_id": "F2", "knowledge_seq": 2, "batch_id": "COMBINED"})
        self.assertEqual(report["sources"]["flights"][0]["batch_id"], None)
        # The later flight departure (12:10) offsets the later scan (11:20): no risk.
        self.assertFalse(report["bags"][0]["connection_at_risk"])
        # Sequence 1 remains on the original scan and flight versions.
        old = archive.build_report(self.archive_path, 1)
        self.assertEqual(old["sources"]["scans"][0]["batch_id"], None)
        self.assertEqual(old["sources"]["flights"][1]["knowledge_seq"], 1)

    def test_archive_survives_reload_and_keeps_all_versions(self):
        self.init()
        archive.apply_batch(self.archive_path, self.write_batch("b1.json", SCAN_CORRECTION))
        archive.apply_batch(self.archive_path, self.write_batch("b2.json", FLIGHT_CORRECTION))
        events = archive._load_events(self.archive_path)
        self.assertEqual([e["knowledge_seq"] for e in events], [1, 2, 3])
        # The original scan payload still exists inside the batch event.
        self.assertEqual(events[1]["scans"][0]["scan"]["event_time"],
                         "2026-01-01T11:20:00+00:00")
        self.assertEqual(archive.build_report(self.archive_path, 1)["knowledge_seq"], 1)

    # ---- CLI -----------------------------------------------------------------

    def test_cli_archive_commands_end_to_end(self):
        out, _ = self.run_cli("archive-init", "--input", str(self.input_path),
                              "--archive", str(self.archive_path))
        self.assertEqual(json.loads(out), {"knowledge_seq": 1})

        out, _ = self.run_cli("archive-apply", "--archive", str(self.archive_path),
                              "--batch", str(self.write_batch("b1.json", SCAN_CORRECTION)))
        self.assertEqual(json.loads(out), {"knowledge_seq": 2, "batch_id": "BATCH-1"})

        report_path = self.root / "r2.json"
        out, _ = self.run_cli("archive-report", "--archive", str(self.archive_path),
                              "--as-of", "2", "--output", str(report_path))
        payload = json.loads(report_path.read_text())
        self.assertTrue(payload["bags"][0]["connection_at_risk"])
        self.assertEqual(payload["knowledge_seq"], 2)
        self.assertEqual(json.loads(out)["connection_risk"], 1)

        # Existing output is not overwritten and the CLI exits 2.
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("archive-report", "--archive", str(self.archive_path),
                         "--as-of", "2", "--output", str(report_path))
        self.assertEqual(ctx.exception.code, 2)

        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("archive-report", "--archive", str(self.archive_path),
                         "--as-of", "99", "--output", str(self.root / "r99.json"))
        self.assertEqual(ctx.exception.code, 2)
        self.assertFalse((self.root / "r99.json").exists())

    def test_cli_init_refuses_existing_archive_with_exit_2(self):
        self.run_cli("archive-init", "--input", str(self.input_path),
                     "--archive", str(self.archive_path))
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("archive-init", "--input", str(self.input_path),
                         "--archive", str(self.archive_path))
        self.assertEqual(ctx.exception.code, 2)

    def test_cli_apply_invalid_batch_exits_2_and_preserves_archive(self):
        self.run_cli("archive-init", "--input", str(self.input_path),
                     "--archive", str(self.archive_path))
        before = (self.archive_path / archive.EVENTS_FILE).read_bytes()
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("archive-apply", "--archive", str(self.archive_path),
                         "--batch", str(self.write_batch("bad.json", {"batch_id": "BAD"})))
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual((self.archive_path / archive.EVENTS_FILE).read_bytes(), before)

    def test_cli_legacy_single_shot_regression(self):
        output = self.root / "legacy.json"
        out, _ = self.run_cli("--input", str(self.input_path), "--output", str(output))
        payload = json.loads(output.read_text())
        self.assertEqual(set(payload), {"bags", "summary"})
        self.assertNotIn("knowledge_seq", payload)
        self.assertFalse(payload["bags"][0]["connection_at_risk"])
        self.assertEqual(json.loads(out)["total"], 1)
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("--input", str(self.input_path), "--output", str(output))
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

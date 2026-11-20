import io
import json
import random
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from baggage import archive, readaudit
from baggage.cli import main


def initial_state(f2_origin="BBB", f2_departure="2026-01-01T11:00:00+00:00",
                  bags=("A", "B", "C"), scans=None):
    return {
        "flights": [
            {"id": "F1", "origin": "AAA", "destination": "BBB",
             "departure": "2026-01-01T08:00:00+00:00",
             "arrival": "2026-01-01T09:00:00+00:00"},
            {"id": "F2", "origin": f2_origin, "destination": "CCC",
             "departure": f2_departure,
             "arrival": "2026-01-01T12:00:00+00:00"},
        ],
        "bags": [
            {"id": bag_id, "flight_id": "F1", "next_flight_id": "F2",
             "minimum_connection_minutes": 30}
            for bag_id in bags
        ],
        "scans": scans or [],
    }


def read(read_id, candidates, captured_at="2026-01-01T10:30:00+00:00"):
    return {"read_id": read_id, "captured_at": captured_at,
            "candidate_bag_ids": list(candidates)}


def batch(reads, station="BBB",
          departure_from="2026-01-01T10:00:00+00:00",
          departure_until="2026-01-01T12:00:00+00:00",
          capture_from="2026-01-01T10:00:00+00:00",
          capture_until="2026-01-01T11:00:00+00:00"):
    return {
        "station": station,
        "departure_from": departure_from,
        "departure_until": departure_until,
        "capture_from": capture_from,
        "capture_until": capture_until,
        "reads": reads,
    }


class ReadAuditFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive_path = self.root / "archive"
        self.counter = 0

    def write_json(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def init(self, state=None):
        state = initial_state() if state is None else state
        archive.init_archive(self.write_json("input.json", state), self.archive_path)
        return self.archive_path

    def batch_file(self, payload):
        self.counter += 1
        return self.write_json(f"batch-{self.counter}.json", payload)

    def reinit(self, state=None):
        import shutil
        if self.archive_path.exists():
            shutil.rmtree(self.archive_path)
        return self.init(state)

    def build(self, payload, decisions=None, as_of=1):
        decisions_path = None
        if decisions is not None:
            decisions_path = self.write_json("decisions.json", decisions)
        return readaudit.build_report(
            self.archive_path, as_of, self.batch_file(payload), decisions_path
        )

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            main(list(argv))
        return out.getvalue().strip(), err.getvalue().strip()

    @staticmethod
    def bag_states(report):
        return {row["bag_id"]: row["evidence_status"] for row in report["bags"]}

    @staticmethod
    def bag_reads(report):
        return {row["bag_id"]: set(row["evidence_read_ids"]) for row in report["bags"]}

    @staticmethod
    def read_status(report):
        return {row["read_id"]: row["status"] for row in report["reads"]}


class AcceptanceScenario(ReadAuditFixtures):
    """The three-case scenario stated in the task contract."""

    def test_no_decisions_covers_two_and_pins_r1_to_b(self):
        self.init()
        report = self.build(batch([read("R1", ["A", "B"]), read("R2", ["A"])]))
        self.assertEqual(report["summary"]["max_coverage"], 2)
        self.assertEqual(report["summary"]["min_missing"], 1)
        self.assertEqual(self.bag_states(report), {
            "A": readaudit.EVIDENCE_CERTAIN,
            "B": readaudit.EVIDENCE_CERTAIN,
            "C": readaudit.EVIDENCE_MISSING,
        })
        # R1 can only be attributed to B in every maximum scheme.
        by_read = {row["read_id"]: row for row in report["reads"]}
        self.assertEqual(by_read["R1"]["allowed_bag_ids"], ["B"])
        self.assertFalse(by_read["R1"]["may_be_idle"])
        self.assertEqual(by_read["R2"]["allowed_bag_ids"], ["A"])
        # The example is one scheme covering two bags.
        self.assertEqual(len(report["example_pairing"]), 2)

    def test_confirming_r1_to_a_drops_coverage_and_strands_b_c(self):
        self.init()
        report = self.build(
            batch([read("R1", ["A", "B"]), read("R2", ["A"])]),
            decisions={"confirm": [{"read_id": "R1", "bag_id": "A",
                                    "proof_ref": "V17"}]},
        )
        self.assertEqual(report["summary"]["max_coverage"], 1)
        self.assertEqual(self.bag_states(report), {
            "A": readaudit.EVIDENCE_CERTAIN,
            "B": readaudit.EVIDENCE_MISSING,
            "C": readaudit.EVIDENCE_MISSING,
        })
        by_read = {row["read_id"]: row for row in report["reads"]}
        # R2 may no longer claim the confirmed bag A, so it is naturally idle.
        self.assertEqual(by_read["R2"]["allowed_bag_ids"], [])
        self.assertEqual(by_read["R2"]["status"], readaudit.STATUS_UNUSED)
        self.assertEqual(by_read["R1"]["status"], readaudit.STATUS_CONFIRMED)
        self.assertEqual(by_read["R1"]["proof_ref"], "V17")
        self.assertEqual(report["decisions"]["confirm"][0]["proof_ref"], "V17")

    def test_r2_candidates_bc_leaves_every_bag_possibly_missing(self):
        self.init()
        report = self.build(batch([read("R1", ["A", "B"]), read("R2", ["B", "C"])]))
        self.assertEqual(report["summary"]["max_coverage"], 2)
        self.assertEqual(self.bag_states(report), {
            "A": readaudit.EVIDENCE_POSSIBLE,
            "B": readaudit.EVIDENCE_POSSIBLE,
            "C": readaudit.EVIDENCE_POSSIBLE,
        })
        # Each bag is genuinely reachable from some exposed read.
        self.assertEqual(self.bag_reads(report), {
            "A": {"R1"}, "B": {"R1", "R2"}, "C": {"R2"},
        })


class TwoSidedCounting(ReadAuditFixtures):
    """More/fewer reads than bags, attributions and idleness."""

    def test_surplus_reads_force_some_read_idle(self):
        self.init()
        # Two bags, three reads all pointing at both bags: one read must idle.
        report = self.build(batch([
            read("R1", ["A", "B"]), read("R2", ["A", "B"]), read("R3", ["A", "B"]),
        ]))
        self.assertEqual(report["summary"]["max_coverage"], 2)
        statuses = self.read_status(report)
        self.assertEqual(statuses, {
            "R1": readaudit.STATUS_OPTIONAL,
            "R2": readaudit.STATUS_OPTIONAL,
            "R3": readaudit.STATUS_OPTIONAL,
        })
        for row in report["reads"]:
            self.assertTrue(row["may_be_idle"])
            self.assertEqual(set(row["allowed_bag_ids"]), {"A", "B"})
        self.assertEqual(self.bag_states(report), {
            "A": readaudit.EVIDENCE_CERTAIN,
            "B": readaudit.EVIDENCE_CERTAIN,
            "C": readaudit.EVIDENCE_MISSING,
        })

    def test_surplus_bags_force_missing_and_always_used_reads(self):
        self.init()
        # One read that can serve either A or B; C has nothing.
        report = self.build(batch([read("R1", ["A", "B"])]))
        self.assertEqual(report["summary"]["max_coverage"], 1)
        self.assertEqual(self.bag_states(report), {
            "A": readaudit.EVIDENCE_POSSIBLE,
            "B": readaudit.EVIDENCE_POSSIBLE,
            "C": readaudit.EVIDENCE_MISSING,
        })
        by_read = {row["read_id"]: row for row in report["reads"]}
        self.assertFalse(by_read["R1"]["may_be_idle"])
        self.assertEqual(by_read["R1"]["status"], readaudit.STATUS_ALWAYS_USED)
        self.assertEqual(by_read["R1"]["allowed_bag_ids"], ["A", "B"])

    def test_disjoint_pair_covers_all_with_no_choice(self):
        self.init()
        report = self.build(batch([read("R1", ["A"]), read("R2", ["B"]),
                                   read("R3", ["C"])]))
        self.assertEqual(report["summary"]["max_coverage"], 3)
        self.assertEqual(report["summary"]["min_missing"], 0)
        self.assertEqual(set(self.bag_states(report).values()),
                         {readaudit.EVIDENCE_CERTAIN})


class ReadIdentity(ReadAuditFixtures):
    def test_identical_content_different_ids_are_two_reads(self):
        self.init()
        same_time = "2026-01-01T10:20:00+00:00"
        report = self.build(batch([
            read("R1", ["A"], same_time),
            read("R2", ["A"], same_time),
        ]))
        # A single bag, two reads with identical candidates and capture time.
        self.assertEqual(report["summary"]["max_coverage"], 1)
        self.assertEqual(self.bag_reads(report), {"A": {"R1", "R2"},
                                                  "B": set(), "C": set()})
        by_read = {row["read_id"]: row for row in report["reads"]}
        self.assertEqual(by_read["R1"]["allowed_bag_ids"], ["A"])
        self.assertEqual(by_read["R2"]["allowed_bag_ids"], ["A"])
        # Only one may be used: at least one read can idle.
        self.assertTrue(by_read["R1"]["may_be_idle"])
        self.assertTrue(by_read["R2"]["may_be_idle"])
        self.assertEqual(self.bag_states(report)["A"], readaudit.EVIDENCE_CERTAIN)


class WindowsAndOffsets(ReadAuditFixtures):
    def test_capture_window_is_half_open(self):
        self.init()
        report = self.build(batch([
            read("AT_FROM", ["A"], "2026-01-01T10:00:00+00:00"),
            read("BEFORE_END", ["B"], "2026-01-01T10:59:59+00:00"),
            read("AT_END", ["C"], "2026-01-01T11:00:00+00:00"),
        ]))
        statuses = self.read_status(report)
        self.assertEqual(statuses["AT_FROM"], readaudit.STATUS_ALWAYS_USED)
        self.assertEqual(statuses["BEFORE_END"], readaudit.STATUS_ALWAYS_USED)
        self.assertEqual(statuses["AT_END"], readaudit.STATUS_OUTSIDE)
        by_read = {row["read_id"]: row for row in report["reads"]}
        # An out-of-window read is listed but never adjudicated.
        self.assertEqual(by_read["AT_END"]["allowed_bag_ids"], [])
        self.assertFalse(by_read["AT_END"]["in_capture_window"])
        # C therefore has no in-window evidence at all.
        self.assertEqual(self.bag_states(report)["C"], readaudit.EVIDENCE_MISSING)

    def test_departure_window_is_half_open(self):
        # F2 departs exactly at the window's right edge -> out of scope.
        state = initial_state(f2_departure="2026-01-01T12:00:00+00:00")
        self.init(state)
        report = self.build(batch([read("R1", ["A"])]))
        self.assertEqual(report["summary"]["scoped_bag_count"], 0)
        self.assertEqual(report["bags"], [])

    def test_equivalent_offsets_compare_by_instant(self):
        # Capture window and read use different but equivalent UTC offsets.
        self.init()
        report = self.build(batch(
            [read("R1", ["A"], "2026-01-01T19:00:00+09:00")],
            capture_from="2026-01-01T10:00:00+00:00",
            capture_until="2026-01-01T11:00:00+00:00",
        ))
        # 19:00+09:00 == 10:00Z, inside [10:00Z, 11:00Z).
        self.assertEqual(self.read_status(report)["R1"], readaudit.STATUS_ALWAYS_USED)

    def test_departure_window_with_equivalent_offset_includes_bag(self):
        self.init()
        # Window expressed in +09:00 that brackets the 11:00Z departure.
        report = self.build(batch(
            [read("R1", ["A"])],
            departure_from="2026-01-01T19:00:00+09:00",
            departure_until="2026-01-01T21:00:00+09:00",
        ))
        self.assertEqual(report["summary"]["scoped_bag_count"], 3)

    def test_empty_candidate_read_is_listed(self):
        self.init()
        report = self.build(batch([read("R1", []), read("R2", ["A"])]))
        by_read = {row["read_id"]: row for row in report["reads"]}
        self.assertEqual(by_read["R1"]["status"], readaudit.STATUS_UNUSED)
        self.assertEqual(by_read["R1"]["allowed_bag_ids"], [])
        self.assertIn("R1", report["summary"]["manual_review_read_ids"])


class CandidateClassification(ReadAuditFixtures):
    def test_unknown_and_out_of_scope_candidates_do_not_reconcile(self):
        # Add a known bag Z on another flight/station so it is known-but-out-of-scope.
        state = initial_state(bags=("A", "B"))
        state["flights"].append(
            {"id": "F9", "origin": "DDD", "destination": "EEE",
             "departure": "2026-01-01T11:00:00+00:00",
             "arrival": "2026-01-01T12:00:00+00:00"})
        state["bags"].append(
            {"id": "Z", "flight_id": "F1", "next_flight_id": "F9",
             "minimum_connection_minutes": 30})
        self.init(state)
        report = self.build(batch([read("R1", ["A", "Z", "NOPE"])]))
        row = {r["read_id"]: r for r in report["reads"]}["R1"]
        self.assertEqual(row["in_scope_candidate_bag_ids"], ["A"])
        self.assertEqual(row["unknown_candidate_bag_ids"], ["NOPE"])
        self.assertEqual(row["out_of_scope_candidate_bag_ids"], ["Z"])
        self.assertEqual(row["allowed_bag_ids"], ["A"])
        # Only A can be covered; B and Z are not in this round.
        self.assertEqual(report["summary"]["max_coverage"], 1)
        self.assertEqual({row["bag_id"] for row in report["bags"]}, {"A", "B",
                                                                     "Z"} & {"A", "B"})

    def test_scan_evidence_and_connection_risk_do_not_participate(self):
        # Scans that would flag connection risk must not change the read audit.
        scan_state = initial_state(scans=[
            {"id": "S1", "bag_id": "A", "station": "BBB",
             "event_time": "2026-01-01T10:50:00+00:00"},
        ])
        self.init(scan_state)
        report = self.build(batch([read("R1", ["A", "B"])]))
        self.assertEqual(report["summary"]["max_coverage"], 1)
        for row in report["bags"]:
            self.assertNotIn("last_scan", row)
            self.assertNotIn("connection_at_risk", row)

    def test_bag_row_names_next_flight_and_source(self):
        self.init()
        report = self.build(batch([read("R1", ["A"])]))
        row = {r["bag_id"]: r for r in report["bags"]}["A"]
        self.assertEqual(row["next_flight"]["flight_id"], "F2")
        self.assertEqual(row["next_flight"]["origin"], "BBB")
        self.assertEqual(row["source"], {"knowledge_seq": 1, "batch_id": None})


class Decisions(ReadAuditFixtures):
    def test_discard_prevents_attribution_even_if_it_costs_coverage(self):
        self.init()
        report = self.build(
            batch([read("R1", ["A", "B"]), read("R2", ["A"])]),
            decisions={"discard": [{"read_id": "R1", "proof_ref": "V18"}]},
        )
        # Without R1 the maximum coverage is 1 (R2->A), not 2.
        self.assertEqual(report["summary"]["max_coverage"], 1)
        by_read = {row["read_id"]: row for row in report["reads"]}
        self.assertEqual(by_read["R1"]["status"], readaudit.STATUS_DISCARDED)
        self.assertEqual(by_read["R1"]["allowed_bag_ids"], [])
        self.assertEqual(by_read["R1"]["proof_ref"], "V18")
        self.assertEqual(self.bag_states(report)["B"], readaudit.EVIDENCE_MISSING)

    def test_confirmed_bag_cannot_take_another_read(self):
        self.init()
        report = self.build(
            batch([read("R1", ["A"]), read("R2", ["A"])]),
            decisions={"confirm": [{"read_id": "R1", "bag_id": "A",
                                    "proof_ref": "V1"}]},
        )
        by_read = {row["read_id"]: row for row in report["reads"]}
        self.assertEqual(by_read["R1"]["status"], readaudit.STATUS_CONFIRMED)
        self.assertEqual(by_read["R2"]["allowed_bag_ids"], [])
        self.assertEqual(self.bag_states(report)["A"], readaudit.EVIDENCE_CERTAIN)

    def test_decision_file_round_trips_proof_refs(self):
        self.init()
        report = self.build(
            batch([read("R1", ["A", "B"]), read("R2", ["A"])]),
            decisions={
                "confirm": [{"read_id": "R1", "bag_id": "B", "proof_ref": "V17"}],
                "discard": [{"read_id": "R2", "proof_ref": "V18"}],
            },
        )
        self.assertEqual(report["decisions"]["confirm"],
                         [{"read_id": "R1", "bag_id": "B", "proof_ref": "V17"}])
        self.assertEqual(report["decisions"]["discard"],
                         [{"read_id": "R2", "proof_ref": "V18"}])

    def test_reconcile_after_human_clears_a_dispute(self):
        # Initially two reads share bags; confirming one read to one bag removes
        # the dispute and the remaining read becomes deterministic.
        self.init()
        before = self.build(batch([read("R1", ["A", "B"]), read("R2", ["B"])]))
        self.assertEqual(self.bag_states(before),
                         {"A": readaudit.EVIDENCE_CERTAIN,
                          "B": readaudit.EVIDENCE_CERTAIN,
                          "C": readaudit.EVIDENCE_MISSING})
        after = self.build(
            batch([read("R1", ["A", "B"]), read("R2", ["B"])]),
            decisions={"confirm": [{"read_id": "R1", "bag_id": "A",
                                    "proof_ref": "V1"}]},
        )
        by_read = {row["read_id"]: row for row in after["reads"]}
        self.assertEqual(by_read["R2"]["allowed_bag_ids"], ["B"])
        self.assertFalse(by_read["R2"]["may_be_idle"])


class DecisionRejections(ReadAuditFixtures):
    def assert_rejected(self, payload, decisions, pattern):
        with self.assertRaisesRegex(ValueError, pattern):
            self.build(payload, decisions=decisions)
        # Rejection never creates the output file.
        output = self.root / "out.json"
        with self.assertRaisesRegex(ValueError, pattern):
            readaudit.write_report(
                self.archive_path, 1,
                self.batch_file(payload),
                output,
                self.write_json("d.json", decisions),
            )
        self.assertFalse(output.exists())

    def test_read_with_two_decisions_rejected(self):
        self.init()
        self.assert_rejected(
            batch([read("R1", ["A", "B"])]),
            {"confirm": [{"read_id": "R1", "bag_id": "A", "proof_ref": "V1"}],
             "discard": [{"read_id": "R1", "proof_ref": "V2"}]},
            "more than one decision",
        )

    def test_two_confirms_for_one_bag_rejected(self):
        self.init()
        self.assert_rejected(
            batch([read("R1", ["A"]), read("R2", ["A"])]),
            {"confirm": [{"read_id": "R1", "bag_id": "A", "proof_ref": "V1"},
                         {"read_id": "R2", "bag_id": "A", "proof_ref": "V2"}]},
            "confirmed by both",
        )

    def test_confirm_must_name_an_in_scope_candidate(self):
        self.init()
        self.assert_rejected(
            batch([read("R1", ["A"])]),
            {"confirm": [{"read_id": "R1", "bag_id": "C", "proof_ref": "V1"}]},
            "not one of its checkable candidates",
        )

    def test_decision_for_out_of_window_read_rejected(self):
        self.init()
        self.assert_rejected(
            batch([read("R1", ["A"],
                        captured_at="2026-01-01T09:00:00+00:00")]),
            {"confirm": [{"read_id": "R1", "bag_id": "A", "proof_ref": "V1"}]},
            "not in the capture window",
        )

    def test_decision_for_unknown_read_rejected(self):
        self.init()
        self.assert_rejected(
            batch([read("R1", ["A"])]),
            {"discard": [{"read_id": "GHOST", "proof_ref": "V1"}]},
            "not in the capture window",
        )

    def test_blank_proof_ref_rejected(self):
        self.init()
        self.assert_rejected(
            batch([read("R1", ["A"])]),
            {"confirm": [{"read_id": "R1", "bag_id": "A", "proof_ref": ""}]},
            "proof_ref must be a non-empty string",
        )

    def test_unknown_decision_field_rejected(self):
        self.init()
        self.assert_rejected(
            batch([read("R1", ["A"])]),
            {"confirm": [{"read_id": "R1", "bag_id": "A", "proof_ref": "V1",
                          "extra": 1}]},
            "unknown field",
        )


class StrictInput(ReadAuditFixtures):
    def _write_text(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def assert_batch_rejected(self, text, pattern):
        self.init()
        output = self.root / "out.json"
        with self.assertRaisesRegex(ValueError, pattern):
            readaudit.write_report(self.archive_path, 1,
                                   self._write_text("bad.json", text), output)
        self.assertFalse(output.exists())

    def test_duplicate_json_key_rejected(self):
        self.assert_batch_rejected(
            '{"station":"BBB","station":"AAA","departure_from":'
            '"2026-01-01T10:00:00+00:00","departure_until":'
            '"2026-01-01T12:00:00+00:00","capture_from":'
            '"2026-01-01T10:00:00+00:00","capture_until":'
            '"2026-01-01T11:00:00+00:00","reads":[]}',
            "duplicate JSON key",
        )

    def test_duplicate_read_id_rejected(self):
        self.init()
        payload = batch([read("R1", ["A"]), read("R1", ["B"])])
        with self.assertRaisesRegex(ValueError, "duplicate read_id"):
            self.build(payload)

    def test_duplicate_candidate_rejected(self):
        self.init()
        payload = batch([read("R1", ["A", "A"])])
        with self.assertRaisesRegex(ValueError, "more than once"):
            self.build(payload)

    def test_wrong_types_rejected(self):
        self.init()
        with self.assertRaisesRegex(ValueError, "must be a list"):
            self.build(batch("not-a-list"))
        payload = batch([read("R1", ["A"])])
        payload["reads"][0]["candidate_bag_ids"] = "AB"
        with self.assertRaisesRegex(ValueError, "must be a list"):
            self.build(payload)
        payload = batch([read("R1", ["A"])])
        payload["reads"][0]["read_id"] = 5
        with self.assertRaisesRegex(ValueError, "non-empty string"):
            self.build(payload)

    def test_empty_station_and_missing_offset_rejected(self):
        self.init()
        with self.assertRaisesRegex(ValueError, "station must be a non-empty"):
            self.build(batch([read("R1", ["A"])], station=""))
        payload = batch([read("R1", ["A"])])
        payload["capture_from"] = "2026-01-01T10:00:00"
        with self.assertRaisesRegex(ValueError, "UTC offset"):
            self.build(payload)

    def test_window_order_must_be_strict(self):
        self.init()
        with self.assertRaisesRegex(ValueError, "strictly before"):
            self.build(batch([], capture_from="2026-01-01T10:00:00+00:00",
                             capture_until="2026-01-01T10:00:00+00:00"))
        with self.assertRaisesRegex(ValueError, "strictly before"):
            self.build(batch([], departure_from="2026-01-01T12:00:00+00:00",
                             departure_until="2026-01-01T10:00:00+00:00"))

    def test_unknown_batch_field_rejected(self):
        self.init()
        payload = batch([read("R1", ["A"])])
        payload["batch_id"] = "X"
        with self.assertRaisesRegex(ValueError, "unknown field"):
            self.build(payload)


class ArchiveSequences(ReadAuditFixtures):
    def test_invalid_sequence_rejected_without_output(self):
        self.init()
        output = self.root / "out.json"
        for bad in (0, 2, -1):
            with self.assertRaisesRegex(ValueError, "unknown or unaccepted"):
                readaudit.write_report(self.archive_path, bad,
                                       self.batch_file(batch([read("R1", ["A"])])),
                                       output)
            self.assertFalse(output.exists())

    def test_existing_output_not_overwritten(self):
        self.init()
        output = self.root / "out.json"
        output.write_text("sentinel", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            readaudit.write_report(self.archive_path, 1,
                                   self.batch_file(batch([read("R1", ["A"])])),
                                   output)
        self.assertEqual(output.read_text(encoding="utf-8"), "sentinel")

    def test_flight_correction_moves_bag_out_of_scope_across_sequences(self):
        self.init()
        # Sequence 1: F2 departs from BBB inside the window -> A in scope.
        seq1 = readaudit.build_report(
            self.archive_path, 1, self.batch_file(batch([read("R1", ["A"])])))
        self.assertEqual(seq1["summary"]["scoped_bag_count"], 3)
        # Correct F2 to depart from DDD (out of scope for station BBB).
        correction = {"batch_id": "MOVE", "flights": [
            {"flight_id": "F2", "origin": "DDD", "destination": "CCC",
             "departure": "2026-01-01T11:00:00+00:00",
             "arrival": "2026-01-01T12:00:00+00:00"}]}
        archive.apply_batch(self.archive_path,
                            self.write_json("move.json", correction))
        seq2 = readaudit.build_report(
            self.archive_path, 2, self.batch_file(batch([read("R1", ["A"])])))
        self.assertEqual(seq2["summary"]["scoped_bag_count"], 0)
        self.assertEqual(seq2["bags"], [])
        # The old sequence keeps the bag in scope with the old source version.
        seq1_again = readaudit.build_report(
            self.archive_path, 1, self.batch_file(batch([read("R1", ["A"])])))
        self.assertEqual(seq1_again["bags"], seq1["bags"])


class Determinism(ReadAuditFixtures):
    def test_reordering_inputs_does_not_change_the_report(self):
        self.init()
        reads = [read("R1", ["A", "B"]), read("R2", ["A"]),
                 read("R3", ["B", "C"]), read("R4", ["C"])]
        decisions = {
            "confirm": [{"read_id": "R1", "bag_id": "B", "proof_ref": "V1"}],
            "discard": [{"read_id": "R4", "proof_ref": "V2"}],
        }
        first = self.build(batch(reads), decisions=decisions)

        shuffled = [read("R3", ["C", "B"]), read("R1", ["B", "A"]),
                    read("R4", ["C"]), read("R2", ["A"])]
        reordered_decisions = {
            "discard": [{"read_id": "R4", "proof_ref": "V2"}],
            "confirm": [{"read_id": "R1", "bag_id": "B", "proof_ref": "V1"}],
        }
        second = self.build(batch(shuffled), decisions=reordered_decisions)

        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))

    def test_example_pairing_is_consistent_with_conclusions(self):
        self.init()
        report = self.build(batch([read("R1", ["A", "B"]), read("R2", ["B", "C"])]))
        used_reads = [p["read_id"] for p in report["example_pairing"]]
        used_bags = [p["bag_id"] for p in report["example_pairing"]]
        self.assertEqual(len(used_reads), len(set(used_reads)))
        self.assertEqual(len(used_bags), len(set(used_bags)))
        self.assertEqual(len(used_reads), report["summary"]["max_coverage"])
        by_read = {row["read_id"]: row for row in report["reads"]}
        for pair in report["example_pairing"]:
            self.assertIn(pair["bag_id"], by_read[pair["read_id"]]["allowed_bag_ids"])

    def test_summary_counters_are_internally_consistent(self):
        self.init()
        report = self.build(
            batch([read("R1", ["A"]), read("R2", ["A", "B"]), read("R3", []),
                   read("R4", ["C"], captured_at="2026-01-01T09:00:00+00:00")]),
            decisions={"confirm": [{"read_id": "R1", "bag_id": "A",
                                    "proof_ref": "V"}],
                       "discard": [{"read_id": "R3", "proof_ref": "W"}]},
        )
        summary = report["summary"]
        read_counts = summary["read_status"]
        statuses = ("confirmed", "discarded", "always_used", "optional", "unused")
        self.assertEqual(read_counts["in_window"],
                         sum(read_counts[s] for s in statuses))
        self.assertEqual(read_counts["in_window"] + read_counts["outside_window"],
                         len(report["reads"]))
        self.assertEqual(sum(summary["bag_evidence"].values()),
                         summary["scoped_bag_count"])
        self.assertEqual(summary["max_coverage"] + summary["min_missing"],
                         summary["scoped_bag_count"])


class OrchestrationVsBruteForce(ReadAuditFixtures):
    """Cross-check every conclusion against exhaustive enumeration on small graphs.

    This guards the Dulmage-Mendelsohn shortcut: the report is not allowed to
    depend on the one example pairing, and "candidate exists" is never treated
    as a usable edge.  A handful of random instances are enumerated exactly.
    """

    def brute(self, bag_ids, reads, confirms, discards):
        live = [r for r in reads if r not in discards]
        forced = dict(confirms)
        taken = set(forced.values())
        options = {}
        for read_id in live:
            if read_id in forced:
                options[read_id] = [forced[read_id]]
            else:
                options[read_id] = [b for b in reads[read_id] if b not in taken]
        best = -1
        schemes = []

        def recurse(index, used, chosen):
            nonlocal best, schemes
            if index == len(live):
                scheme = list(chosen)
                if len(scheme) > best:
                    best = len(scheme)
                    schemes = [scheme]
                elif len(scheme) == best:
                    schemes.append(scheme)
                return
            read_id = live[index]
            recurse(index + 1, used, chosen)
            for bag_id in options[read_id]:
                if bag_id in used:
                    continue
                chosen.append((read_id, bag_id))
                used.add(bag_id)
                recurse(index + 1, used, chosen)
                used.discard(bag_id)
                chosen.pop()

        recurse(0, set(), [])
        total = len(schemes)
        bag_count = {b: 0 for b in bag_ids}
        read_count = {r: 0 for r in live}
        for scheme in schemes:
            covered = {b for _r, b in scheme}
            used = {r for r, _b in scheme}
            for bag_id in bag_ids:
                bag_count[bag_id] += bag_id in covered
            for read_id in live:
                read_count[read_id] += read_id in used
        bag_states = {
            b: (readaudit.EVIDENCE_MISSING if bag_count[b] == 0
                else readaudit.EVIDENCE_CERTAIN if bag_count[b] == total
                else readaudit.EVIDENCE_POSSIBLE)
            for b in bag_ids
        }
        return best, bag_states, read_count, total

    def test_random_small_instances_match_brute_force(self):
        rng = random.Random(20260301)
        for trial in range(40):
            bag_ids = [f"B{i}" for i in range(rng.randint(1, 4))]
            read_ids = [f"R{i}" for i in range(rng.randint(1, 4))]
            reads = {}
            rows = []
            for read_id in read_ids:
                candidates = sorted(rng.sample(
                    bag_ids, rng.randint(0, len(bag_ids))))
                reads[read_id] = candidates
                rows.append(read(read_id, candidates))
            confirms = {}
            for read_id in read_ids:
                if reads[read_id] and rng.random() < 0.2:
                    confirms[read_id] = reads[read_id][0]
            # keep one confirmation per bag
            seen = set()
            confirms = {r: b for r, b in confirms.items()
                        if not (b in seen or seen.add(b))}
            discards = {r for r in read_ids
                        if r not in confirms and rng.random() < 0.2}
            state = initial_state(bags=tuple(bag_ids))
            self.reinit(state)
            decisions = {}
            if confirms:
                decisions["confirm"] = [
                    {"read_id": r, "bag_id": b, "proof_ref": "V"}
                    for r, b in confirms.items()]
            if discards:
                decisions["discard"] = [
                    {"read_id": r, "proof_ref": "V"} for r in discards]
            report = self.build(
                batch(rows), decisions=decisions if decisions else None)

            best, bag_states, read_count, total = self.brute(
                bag_ids, reads, confirms, discards)
            self.assertEqual(report["summary"]["max_coverage"], best,
                             f"trial {trial}: {reads} {confirms} {discards}")
            self.assertEqual(self.bag_states(report), bag_states,
                             f"trial {trial}: {reads} {confirms} {discards}")
            by_read = {row["read_id"]: row for row in report["reads"]}
            for read_id in read_ids:
                if read_id in discards:
                    self.assertEqual(by_read[read_id]["status"],
                                     readaudit.STATUS_DISCARDED)
                    continue
                if read_id in confirms:
                    self.assertEqual(by_read[read_id]["status"],
                                     readaudit.STATUS_CONFIRMED)
                    continue
                count = read_count[read_id]
                if count == 0:
                    expected = readaudit.STATUS_UNUSED
                elif count == total:
                    expected = readaudit.STATUS_ALWAYS_USED
                else:
                    expected = readaudit.STATUS_OPTIONAL
                self.assertEqual(by_read[read_id]["status"], expected,
                                 f"trial {trial} read {read_id}")


class Cli(ReadAuditFixtures):
    def test_cli_end_to_end_and_exit_codes(self):
        self.init()
        batch_path = self.batch_file(batch([read("R1", ["A", "B"])]))
        output = self.root / "report.json"
        out, _ = self.run_cli("read-audit", "--archive", str(self.archive_path),
                              "--as-of", "1", "--input", batch_path,
                              "--output", str(output))
        summary = json.loads(out)
        self.assertEqual(summary["max_coverage"], 1)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["format"], readaudit.REPORT_FORMAT)

        # Existing output -> exit 2 and unchanged bytes.
        before = output.read_bytes()
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("read-audit", "--archive", str(self.archive_path),
                         "--as-of", "1", "--input", batch_path,
                         "--output", str(output))
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(output.read_bytes(), before)

        # Invalid sequence -> exit 2, no file.
        missing = self.root / "missing.json"
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("read-audit", "--archive", str(self.archive_path),
                         "--as-of", "9", "--input", batch_path,
                         "--output", str(missing))
        self.assertEqual(ctx.exception.code, 2)
        self.assertFalse(missing.exists())

    def test_cli_with_decisions(self):
        self.init()
        batch_path = self.batch_file(batch([read("R1", ["A"])]))
        decisions = self.write_json("d.json", {
            "confirm": [{"read_id": "R1", "bag_id": "A", "proof_ref": "V9"}]})
        output = self.root / "report.json"
        self.run_cli("read-audit", "--archive", str(self.archive_path),
                     "--as-of", "1", "--input", batch_path,
                     "--decisions", decisions, "--output", str(output))
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["decisions"]["confirm"][0]["proof_ref"], "V9")


if __name__ == "__main__":
    unittest.main()

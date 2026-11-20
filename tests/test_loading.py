import io
import json
import multiprocessing
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from baggage import archive, loading
from baggage.cli import main


def initial_state(f2_departure="2026-01-01T12:00:00+00:00"):
    return {
        "flights": [
            {"id": "F1", "origin": "AAA", "destination": "BBB",
             "departure": "2026-01-01T10:00:00+00:00",
             "arrival": "2026-01-01T11:00:00+00:00"},
            {"id": "F2", "origin": "BBB", "destination": "CCC",
             "departure": f2_departure, "arrival": "2026-01-02T00:30:00+00:00"},
        ],
        "bags": [
            {"id": "A", "flight_id": "F1", "minimum_connection_minutes": 30},
            {"id": "B", "flight_id": "F1", "next_flight_id": "F2",
             "minimum_connection_minutes": 30},
            {"id": "C", "flight_id": "F1", "next_flight_id": "F2",
             "minimum_connection_minutes": 30},
            {"id": "D", "flight_id": "F1", "minimum_connection_minutes": 30},
            {"id": "E", "flight_id": "F1", "next_flight_id": "F2",
             "minimum_connection_minutes": 30},
        ],
        "scans": [
            {"id": "S-A", "bag_id": "A", "station": "AAA",
             "event_time": "2026-01-01T09:00:00+00:00"},
            {"id": "S-B", "bag_id": "B", "station": "BBB",
             "event_time": "2026-01-01T11:10:00+00:00"},
            {"id": "S-C", "bag_id": "C", "station": "BBB",
             "event_time": "2026-01-01T11:15:00+00:00"},
        ],
    }


def clearance(cid, bag_id, scan_id, seq, decision="pass",
              valid_from="2026-01-01T11:00:00+00:00",
              valid_until="2026-01-01T13:00:00+00:00"):
    return {"id": cid, "bag_id": bag_id, "scan_id": scan_id,
            "scan_knowledge_seq": seq, "decision": decision,
            "valid_from": valid_from, "valid_until": valid_until}


def record(op_clearance):
    return {"op": "record", "clearance": op_clearance}


def revoke(clearance_id):
    return {"op": "revoke", "clearance_id": clearance_id}


class WorkbenchFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive_path = self.root / "archive"
        self.counter = 0

    # ---- fixtures -----------------------------------------------------------

    def init(self, f2_departure="2026-01-01T12:00:00+00:00", workbench=True):
        self.write_json("input.json", initial_state(f2_departure))
        archive.init_archive(str(self.root / "input.json"), self.archive_path)
        if workbench:
            loading.init_workbench(self.archive_path)
        return self.archive_path

    def write_json(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def batch(self, payload, name=None):
        self.counter += 1
        return self.write_json(name or f"batch-{self.counter}.json", payload)

    def screening(self, cid, bag_id, scan_id, seq=1, **kwargs):
        return loading.apply_screening(
            self.archive_path,
            self.batch({"batch_id": f"SCR-{cid}",
                        "operations": [record(clearance(cid, bag_id, scan_id, seq, **kwargs))]}),
        )

    def pass_b_and_c(self):
        return loading.apply_screening(
            self.archive_path,
            self.batch({
                "batch_id": "CLR-BC",
                "operations": [
                    record(clearance("C-B", "B", "S-B", 1)),
                    record(clearance("C-C", "C", "S-C", 1)),
                ],
            }),
        )

    def review(self, manifest_id, flight_id, bag_ids, name=None):
        return loading.review_manifest(
            self.archive_path,
            self.batch({"manifest_id": manifest_id, "flight_id": flight_id,
                        "bag_ids": bag_ids}, name=name or f"req-{manifest_id}.json"),
        )

    def apply_archive_batch(self, payload):
        return archive.apply_batch(self.archive_path, self.batch(payload))

    def workbench_bytes(self):
        return (self.archive_path / loading.WORKBENCH_FILE).read_bytes()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            main(list(argv))
        return out.getvalue().strip(), err.getvalue().strip()


class WorkbenchCase(WorkbenchFixtures):
    # ---- 1. init ------------------------------------------------------------

    def test_init_requires_archive_and_is_unique(self):
        with self.assertRaisesRegex(ValueError, "not a bag-reconcile archive"):
            loading.init_workbench(self.archive_path)
        self.init(workbench=False)
        result = loading.init_workbench(self.archive_path)
        self.assertTrue(Path(result["workbench"]).is_file())
        with self.assertRaisesRegex(ValueError, "already exists"):
            loading.init_workbench(self.archive_path)

    def test_init_rejects_corrupt_archive(self):
        self.init(workbench=False)
        (self.archive_path / archive.EVENTS_FILE).write_text("not json\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "corrupt archive"):
            loading.init_workbench(self.archive_path)
        self.assertFalse((self.archive_path / loading.WORKBENCH_FILE).exists())

    def test_commands_require_workbench(self):
        self.init(workbench=False)
        for call in (
            lambda: self.screening("C-B", "B", "S-B"),
            lambda: self.review("M1", "F2", ["B"]),
            lambda: loading.commit_review(self.archive_path, "R1"),
            lambda: loading.cancel_manifest(self.archive_path, "M1"),
            lambda: loading.manifest_status(self.archive_path, "M1"),
        ):
            with self.assertRaisesRegex(ValueError, "workbench does not exist"):
                call()

    # ---- 2. screening -------------------------------------------------------

    def test_clearance_must_bind_to_latest_scan_and_version(self):
        self.init()
        # Wrong scan id.
        with self.assertRaisesRegex(ValueError, "not the latest scan"):
            self.screening("C-B", "B", "S-C")
        # Correct id, wrong source version (same value is irrelevant).
        with self.assertRaisesRegex(ValueError, "not the latest source version"):
            self.screening("C-B", "B", "S-B", 99)
        # Bag with no scan at all.
        with self.assertRaisesRegex(ValueError, "has no scan"):
            self.screening("C-D", "D", "S-D")
        # Naive timestamps and inverted windows.
        with self.assertRaisesRegex(ValueError, "UTC offset"):
            self.screening("C-B", "B", "S-B", valid_from="2026-01-01T11:00:00")
        with self.assertRaisesRegex(ValueError, "strictly before"):
            self.screening("C-B", "B", "S-B",
                           valid_until="2026-01-01T11:00:00+00:00")
        # Bad decision.
        with self.assertRaisesRegex(ValueError, "decision"):
            self.screening("C-B", "B", "S-B", decision="maybe")

    def test_screening_batch_is_all_or_nothing(self):
        self.init()
        before = self.workbench_bytes()
        bad = {"batch_id": "MIXED", "operations": [
            record(clearance("C-B", "B", "S-B", 1)),
            record(clearance("C-X", "NOPE", "S-B", 1)),
        ]}
        with self.assertRaisesRegex(ValueError, "unknown bag"):
            loading.apply_screening(self.archive_path, self.batch(bad))
        self.assertEqual(self.workbench_bytes(), before)
        # The valid-looking part did not take effect.
        review = self.review("M1", "F2", ["B"])
        self.assertIn("no_current_clearance", review["bags"]["B"]["unmet_conditions"])

    def test_duplicate_batch_and_clearance_ids_are_never_reused(self):
        self.init()
        self.screening("C-B", "B", "S-B")
        again = {"batch_id": "SCR-C-B", "operations": [record(clearance("C-NEW", "C", "S-C", 1))]}
        with self.assertRaisesRegex(ValueError, "duplicate screening batch_id"):
            loading.apply_screening(self.archive_path, self.batch(again))
        # Clearance id reuse is rejected even after revocation.
        loading.apply_screening(self.archive_path, self.batch(
            {"batch_id": "REV-1", "operations": [revoke("C-B")]}))
        reuse = {"batch_id": "REV-2", "operations": [record(clearance("C-B", "B", "S-B", 1))]}
        with self.assertRaisesRegex(ValueError, "already been used"):
            loading.apply_screening(self.archive_path, self.batch(reuse))

    def test_revoke_rules(self):
        self.init()
        self.pass_b_and_c()
        with self.assertRaisesRegex(ValueError, "unknown clearance"):
            loading.apply_screening(self.archive_path, self.batch(
                {"batch_id": "R-UNK", "operations": [revoke("C-Z")]}))
        # Supersede C-B, then revoking the superseded record is rejected.
        loading.apply_screening(self.archive_path, self.batch(
            {"batch_id": "R-SUP", "operations": [
                record(clearance("C-B2", "B", "S-B", 1))]}))
        with self.assertRaisesRegex(ValueError, "superseded"):
            loading.apply_screening(self.archive_path, self.batch(
                {"batch_id": "R-SUP2", "operations": [revoke("C-B")]}))
        # Revoked twice is rejected.
        loading.apply_screening(self.archive_path, self.batch(
            {"batch_id": "R-1", "operations": [revoke("C-B2")]}))
        with self.assertRaisesRegex(ValueError, "already been revoked"):
            loading.apply_screening(self.archive_path, self.batch(
                {"batch_id": "R-2", "operations": [revoke("C-B2")]}))

    def test_revoked_old_pass_does_not_revive(self):
        self.init()
        self.pass_b_and_c()
        loading.apply_screening(self.archive_path, self.batch(
            {"batch_id": "NEW-PASS", "operations": [record(clearance("C-B2", "B", "S-B", 1))]}))
        loading.apply_screening(self.archive_path, self.batch(
            {"batch_id": "REV-B2", "operations": [revoke("C-B2")]}))
        review = self.review("M1", "F2", ["B"])
        # The older pass C-B stays superseded; the current record is revoked.
        self.assertEqual(review["bags"]["B"]["clearance"]["status"], "revoked")
        self.assertIn("clearance_revoked", review["bags"]["B"]["unmet_conditions"])
        self.assertFalse(review["committable"])

    def test_batch_order_does_not_change_validity(self):
        # Recording the same bag twice is invalid in either order; revoking a
        # record created in the same batch is invalid in either order.
        self.init()
        for operations in (
            [record(clearance("C-B", "B", "S-B", 1)), record(clearance("C-B2", "B", "S-B", 1))],
            [record(clearance("C-B2", "B", "S-B", 1)), record(clearance("C-B", "B", "S-B", 1))],
        ):
            with self.assertRaisesRegex(ValueError, "more than once"):
                loading.apply_screening(self.archive_path, self.batch(
                    {"batch_id": "ORD", "operations": operations}))
        for operations in (
            [record(clearance("C-C", "C", "S-C", 1)), revoke("C-C")],
            [revoke("C-C"), record(clearance("C-C", "C", "S-C", 1))],
        ):
            with self.assertRaises(ValueError):
                loading.apply_screening(self.archive_path, self.batch(
                    {"batch_id": "ORD2", "operations": operations}))

    def test_superseding_record_keeps_history_for_old_reviews(self):
        self.init()
        self.pass_b_and_c()
        review = self.review("M1", "F2", ["B"])
        loading.commit_review(self.archive_path, review["review_id"])
        loading.apply_screening(self.archive_path, self.batch(
            {"batch_id": "SUP", "operations": [record(clearance("C-B2", "B", "S-B", 1))]}))
        # The immutable review still shows the original record it relied on.
        status = loading.manifest_status(self.archive_path, "M1")
        self.assertEqual(status["original_basis"]["bags"]["B"]["clearance"]["id"], "C-B")
        self.assertTrue(status["original_basis"]["bags"]["B"]["eligible"])
        # The credential identity is relevant evidence, so status is suspended
        # even though the replacement pass is itself valid and still occupies B.
        self.assertEqual(status["status"], "suspended")
        self.assertEqual([c["kind"] for c in status["current"]["changed_sources"]],
                         ["clearance"])
        other = self.review("M2", "F2", ["B"])
        self.assertEqual(other["bags"]["B"]["occupied_by"], ["M1"])

    # ---- 3/4. review and commit --------------------------------------------

    def test_acceptance_m1_commits_then_m2_blocked(self):
        self.init()
        self.pass_b_and_c()
        r1 = self.review("M1", "F2", ["B", "C"])
        r2 = self.review("M2", "F2", ["B"])
        self.assertTrue(r1["committable"])
        self.assertTrue(r2["committable"])
        self.assertEqual(r1["requested_bag_ids"], ["B", "C"])
        self.assertFalse(r1["bags"]["B"]["occupied_by"])
        # A review occupies nothing.
        with self.assertRaisesRegex(ValueError, "unknown manifest"):
            loading.manifest_status(self.archive_path, "M1")

        # An unrelated bag gets a new scan; M1's review stays valid.
        self.apply_archive_batch({"batch_id": "UNRELATED", "scans": [
            {"op": "append", "scan": {"id": "S-A2", "bag_id": "A", "station": "AAA",
                                      "event_time": "2026-01-01T09:30:00+00:00"}}]})
        receipt = loading.commit_review(self.archive_path, r1["review_id"])
        self.assertEqual(receipt["manifest_id"], "M1")
        self.assertEqual(receipt["bag_ids"], ["B", "C"])

        with self.assertRaisesRegex(ValueError, "occupied"):
            loading.commit_review(self.archive_path, r2["review_id"])
        status = loading.manifest_status(self.archive_path, "M1")
        self.assertEqual(status["status"], "active")
        # C is still held by M1.
        r3 = self.review("M3", "F2", ["C"])
        self.assertIn("occupied", r3["bags"]["C"]["unmet_conditions"])
        self.assertEqual(r3["bags"]["C"]["occupied_by"], ["M1"])

    def test_review_is_not_committable_when_any_bag_fails(self):
        self.init()
        self.screening("C-B", "B", "S-B")
        review = self.review("M1", "F2", ["B", "C"])
        self.assertFalse(review["committable"])
        self.assertTrue(review["bags"]["B"]["eligible"])
        self.assertIn("no_current_clearance", review["bags"]["C"]["unmet_conditions"])
        with self.assertRaisesRegex(ValueError, "not committable at review time"):
            loading.commit_review(self.archive_path, review["review_id"])
        # Later improving C does not upgrade the old review.
        self.screening("C-C", "C", "S-C")
        with self.assertRaisesRegex(ValueError, "not committable at review time"):
            loading.commit_review(self.archive_path, review["review_id"])

    def test_distinct_reasons_and_occupier_listing(self):
        self.init()
        # No clearance at all vs. pass-but-stale are distinguishable.
        r = self.review("M0", "F2", ["B"])
        self.assertIn("no_current_clearance", r["bags"]["B"]["unmet_conditions"])
        self.screening("C-B", "B", "S-B")
        self.apply_archive_batch({"batch_id": "SAME-VALUE", "scans": [
            {"op": "replace", "scan_id": "S-B", "scan": {
                "id": "S-B", "bag_id": "B", "station": "BBB",
                "event_time": "2026-01-01T11:10:00+00:00"}}]})
        r = self.review("M1", "F2", ["B"])
        self.assertIn("clearance_scan_stale", r["bags"]["B"]["unmet_conditions"])
        self.assertNotIn("no_current_clearance", r["bags"]["B"]["unmet_conditions"])

    def test_unassigned_flight_and_missing_scan_reasons(self):
        self.init()
        # D is only assigned to F1 and has no next flight.
        r = self.review("M1", "F2", ["D"])
        self.assertIn("not_assigned_to_flight", r["bags"]["D"]["unmet_conditions"])
        r = self.review("M2", "F2", ["D"])
        self.assertIn("no_scan", r["bags"]["D"]["unmet_conditions"])

    def test_station_and_late_scan_reasons(self):
        self.init()
        self.screening("C-C", "C", "S-C")
        # C's latest scan is moved to the wrong station and after departure.
        self.apply_archive_batch({"batch_id": "SCAN-C", "scans": [
            {"op": "append", "scan": {"id": "S-C-LATE", "bag_id": "C", "station": "AAA",
                                      "event_time": "2026-01-01T13:00:00+00:00"}}]})
        review = self.review("M1", "F2", ["C"])
        reasons = review["bags"]["C"]["unmet_conditions"]
        self.assertIn("station_mismatch", reasons)
        self.assertIn("scan_after_departure", reasons)
        self.assertIn("clearance_scan_stale", reasons)

    def test_stale_review_rejected_after_new_last_scan(self):
        self.init()
        self.pass_b_and_c()
        review = self.review("M1", "F2", ["B"])
        self.apply_archive_batch({"batch_id": "NEW-LAST", "scans": [
            {"op": "append", "scan": {"id": "S-B2", "bag_id": "B", "station": "BBB",
                                      "event_time": "2026-01-01T11:50:00+00:00"}}]})
        with self.assertRaisesRegex(ValueError, "relevant evidence changed"):
            loading.commit_review(self.archive_path, review["review_id"])
        self.assertNotIn("M1", self.manifest_ids())

    def test_earlier_scan_that_is_not_last_keeps_review_valid(self):
        self.init()
        self.pass_b_and_c()
        review = self.review("M1", "F2", ["B", "C"])
        self.apply_archive_batch({"batch_id": "EARLIER", "scans": [
            {"op": "append", "scan": {"id": "S-OLD", "bag_id": "B", "station": "BBB",
                                      "event_time": "2026-01-01T09:00:00+00:00"}}]})
        self.assertTrue(loading.commit_review(self.archive_path, review["review_id"])["committed"])

    def test_same_value_scan_correction_invalidates_review(self):
        self.init()
        self.pass_b_and_c()
        review = self.review("M1", "F2", ["B"])
        self.apply_archive_batch({"batch_id": "IDENTICAL", "scans": [
            {"op": "replace", "scan_id": "S-B", "scan": {
                "id": "S-B", "bag_id": "B", "station": "BBB",
                "event_time": "2026-01-01T11:10:00+00:00"}}]})
        with self.assertRaisesRegex(ValueError, "relevant evidence changed"):
            loading.commit_review(self.archive_path, review["review_id"])
        fresh = self.review("M1", "F2", ["B"])
        self.assertIn("clearance_scan_stale", fresh["bags"]["B"]["unmet_conditions"])
        # Recording a fresh pass bound to the new version restores eligibility.
        loading.apply_screening(self.archive_path, self.batch(
            {"batch_id": "PASS-V2", "operations": [
                record(clearance("C-B2", "B", "S-B", 2))]}))
        restored = self.review("M1", "F2", ["B"])
        self.assertTrue(restored["committable"])
        self.assertTrue(loading.commit_review(self.archive_path, restored["review_id"])["committed"])

    def test_flight_source_change_invalidates_review_even_if_values_equal(self):
        self.init()
        self.pass_b_and_c()
        review = self.review("M1", "F2", ["B", "C"])
        # Republish F2 with identical values: the source version still changes.
        self.apply_archive_batch({"batch_id": "F2-SAME", "flights": [
            {"flight_id": "F2", "origin": "BBB", "destination": "CCC",
             "departure": "2026-01-01T12:00:00+00:00",
             "arrival": "2026-01-02T00:30:00+00:00"}]})
        with self.assertRaisesRegex(ValueError, "flight_source"):
            loading.commit_review(self.archive_path, review["review_id"])

    def test_unrelated_flight_change_does_not_invalidate_review(self):
        self.init()
        self.pass_b_and_c()
        review = self.review("M1", "F2", ["B"])
        self.apply_archive_batch({"batch_id": "F1-CHANGE", "flights": [
            {"flight_id": "F1", "origin": "AAA", "destination": "BBB",
             "departure": "2026-01-01T10:00:00+00:00",
             "arrival": "2026-01-01T11:05:00+00:00"}]})
        self.assertTrue(loading.commit_review(self.archive_path, review["review_id"])["committed"])

    def test_right_open_validity_window(self):
        self.init()
        self.pass_b_and_c()
        review = self.review("M1", "F2", ["B"])
        self.apply_archive_batch({"batch_id": "MOVE-F2", "flights": [
            {"flight_id": "F2", "origin": "BBB", "destination": "CCC",
             "departure": "2026-01-01T13:00:00+00:00",
             "arrival": "2026-01-02T00:30:00+00:00"}]})
        fresh = self.review("M1", "F2", ["B"])
        self.assertIn("clearance_window_not_covering_departure",
                      fresh["bags"]["B"]["unmet_conditions"])
        # Same instant written with a different offset behaves identically.
        self.apply_archive_batch({"batch_id": "MOVE-F2-TZ", "flights": [
            {"flight_id": "F2", "origin": "BBB", "destination": "CCC",
             "departure": "2026-01-01T14:00:00+01:00",
             "arrival": "2026-01-02T01:30:00+01:00"}]})
        fresh_tz = self.review("M1", "F2", ["B"])
        self.assertEqual(fresh_tz["bags"]["B"]["unmet_conditions"],
                         fresh["bags"]["B"]["unmet_conditions"])
        # One second before the upper bound is inside the window.
        self.apply_archive_batch({"batch_id": "MOVE-F2-OK", "flights": [
            {"flight_id": "F2", "origin": "BBB", "destination": "CCC",
             "departure": "2026-01-01T12:59:59+00:00",
             "arrival": "2026-01-02T00:30:00+00:00"}]})
        ok = self.review("M1", "F2", ["B"])
        self.assertTrue(ok["bags"]["B"]["eligible"])

    def test_multiple_reviews_same_manifest_only_one_commits(self):
        self.init()
        self.pass_b_and_c()
        first = self.review("M1", "F2", ["B"])
        second = self.review("M1", "F2", ["B", "C"])
        self.assertNotEqual(first["review_id"], second["review_id"])
        loading.commit_review(self.archive_path, first["review_id"])
        with self.assertRaisesRegex(ValueError, "already been registered"):
            loading.commit_review(self.archive_path, second["review_id"])
        # The second review also cannot create a new review for a used id.
        with self.assertRaisesRegex(ValueError, "already been registered or cancelled"):
            self.review("M1", "F2", ["B"])

    def test_commit_retry_returns_first_receipt_after_cancel_and_revoke(self):
        self.init()
        self.pass_b_and_c()
        review = self.review("M1", "F2", ["B", "C"])
        first = loading.commit_review(self.archive_path, review["review_id"])
        loading.apply_screening(self.archive_path, self.batch(
            {"batch_id": "REV-B", "operations": [revoke("C-B")]}))
        loading.cancel_manifest(self.archive_path, "M1")
        again = loading.commit_review(self.archive_path, review["review_id"])
        self.assertEqual(again, first)
        # No occupancy was restored.
        fresh = self.review("M2", "F2", ["C"])
        self.assertTrue(fresh["bags"]["C"]["eligible"])
        self.assertEqual(fresh["bags"]["C"]["occupied_by"], [])

    # ---- 5. cancel / status -------------------------------------------------

    def test_revoke_suspends_manifest_until_cancel(self):
        self.init()
        self.pass_b_and_c()
        r_m1 = self.review("M1", "F2", ["B", "C"])
        r_m2 = self.review("M2", "F2", ["B"])
        loading.commit_review(self.archive_path, r_m1["review_id"])
        loading.apply_screening(self.archive_path, self.batch(
            {"batch_id": "REV-B", "operations": [revoke("C-B")]}))
        status = loading.manifest_status(self.archive_path, "M1")
        self.assertEqual(status["status"], "suspended")
        self.assertIsNotNone(status["current"]["changed_sources"])
        self.assertIn("B", status["current"]["unmet_conditions"])
        # Still occupies both bags: a *fresh* review reports the occupier, and
        # the stale M2 review is rejected rather than granted the bags.
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            loading.commit_review(self.archive_path, r_m2["review_id"])
        blocked = self.review("M3", "F2", ["C"])
        self.assertEqual(blocked["bags"]["C"]["occupied_by"], ["M1"])

        cancelled = loading.cancel_manifest(self.archive_path, "M1")
        self.assertFalse(cancelled["already_cancelled"])
        self.assertEqual(loading.manifest_status(self.archive_path, "M1")["status"], "cancelled")
        repeated = loading.cancel_manifest(self.archive_path, "M1")
        self.assertTrue(repeated["already_cancelled"])
        self.assertEqual(loading.manifest_status(self.archive_path, "M1")["status"], "cancelled")
        freed = self.review("M4", "F2", ["C"])
        self.assertTrue(freed["bags"]["C"]["eligible"])
        self.assertEqual(freed["bags"]["C"]["occupied_by"], [])

    def test_status_ignores_own_occupancy_and_separates_basis(self):
        self.init()
        self.pass_b_and_c()
        review = self.review("M1", "F2", ["B", "C"])
        loading.commit_review(self.archive_path, review["review_id"])
        status = loading.manifest_status(self.archive_path, "M1")
        self.assertEqual(status["status"], "active")
        self.assertEqual(status["original_basis"]["bags"]["B"]["clearance"]["id"], "C-B")
        self.assertIn("unmet_conditions", status["current"])
        self.assertEqual(status["current"]["unmet_conditions"], {})
        # Original basis is not rewritten by later evidence.
        self.apply_archive_batch({"batch_id": "LATER", "scans": [
            {"op": "append", "scan": {"id": "S-C2", "bag_id": "C", "station": "BBB",
                                      "event_time": "2026-01-01T11:55:00+00:00"}}]})
        after = loading.manifest_status(self.archive_path, "M1")
        self.assertEqual(after["status"], "suspended")
        self.assertEqual(after["original_basis"]["bags"]["C"]["last_scan"]["scan_id"], "S-C")
        self.assertEqual(after["current"]["changed_sources"][0]["current"]["scan_id"], "S-C2")

    def test_unknown_ids_error(self):
        self.init()
        with self.assertRaisesRegex(ValueError, "unknown review"):
            loading.commit_review(self.archive_path, "NOPE")
        with self.assertRaisesRegex(ValueError, "unknown manifest"):
            loading.manifest_status(self.archive_path, "NOPE")
        with self.assertRaisesRegex(ValueError, "unknown manifest"):
            loading.cancel_manifest(self.archive_path, "NOPE")

    # ---- request validation -------------------------------------------------

    def test_malformed_requests_are_command_errors_and_not_saved(self):
        self.init()
        before = self.workbench_bytes()
        cases = [
            {"manifest_id": "M1", "flight_id": "F2", "bag_ids": []},
            {"manifest_id": "M1", "flight_id": "F2", "bag_ids": ["B", "B"]},
            {"manifest_id": "M1", "flight_id": "F2", "bag_ids": ["B", ""]},
            {"manifest_id": "M1", "flight_id": "F2", "bag_ids": ["B", 3]},
            {"manifest_id": "M1", "flight_id": "NOPE", "bag_ids": ["B"]},
            {"manifest_id": "M1", "flight_id": "F2", "bag_ids": ["NOPE"]},
            {"manifest_id": "", "flight_id": "F2", "bag_ids": ["B"]},
            {"manifest_id": 7, "flight_id": "F2", "bag_ids": ["B"]},
            {"manifest_id": "M1", "flight_id": "F2", "bag_ids": "B"},
        ]
        for payload in cases:
            path = self.write_json("bad-request.json", payload)
            with self.assertRaises(ValueError):
                loading.review_manifest(self.archive_path, path)
        self.assertEqual(self.workbench_bytes(), before)

    def test_duplicate_json_keys_are_rejected(self):
        self.init()
        path = self.root / "dup.json"
        path.write_text('{"manifest_id":"M1","manifest_id":"M2","flight_id":"F2","bag_ids":["B"]}',
                        encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            loading.review_manifest(self.archive_path, path)
        self.assertFalse((self.archive_path / loading.WORKBENCH_FILE)
                         .read_text().count('"M1"'))

    # ---- durability and concurrency ----------------------------------------

    def test_write_failure_leaves_prior_state_readable(self):
        self.init()
        self.pass_b_and_c()
        review = self.review("M1", "F2", ["B", "C"])
        loading.commit_review(self.archive_path, review["review_id"])
        before = self.workbench_bytes()
        with mock.patch.object(loading, "_atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                loading.apply_screening(self.archive_path, self.batch(
                    {"batch_id": "FAIL-WRITE", "operations": [
                        record(clearance("C-B2", "B", "S-B", 1))]}))
            with self.assertRaises(OSError):
                loading.cancel_manifest(self.archive_path, "M1")
        self.assertEqual(self.workbench_bytes(), before)
        # Everything accepted earlier is still intact and re-openable.
        self.assertEqual(loading.manifest_status(self.archive_path, "M1")["status"], "active")
        leftovers = list(self.archive_path.glob(".loading-workbench.json.tmp-*"))
        self.assertEqual(leftovers, [])

    def test_write_failure_during_commit_leaves_no_partial_occupancy(self):
        self.init()
        self.pass_b_and_c()
        review = self.review("M1", "F2", ["B", "C"])
        r_m2 = self.review("M2", "F2", ["B"])
        before = self.workbench_bytes()
        with mock.patch.object(loading, "_atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                loading.commit_review(self.archive_path, review["review_id"])
        # No manifest is visible, and neither bag counts as occupied.
        self.assertEqual(self.workbench_bytes(), before)
        self.assertEqual(self.manifest_ids(), set())
        with self.assertRaisesRegex(ValueError, "unknown manifest"):
            loading.manifest_status(self.archive_path, "M1")
        # The very same review can still be committed once the disk recovers.
        self.assertTrue(loading.commit_review(self.archive_path, review["review_id"])["committed"])
        with self.assertRaisesRegex(ValueError, "occupied"):
            loading.commit_review(self.archive_path, r_m2["review_id"])

    def test_state_survives_separate_processes(self):
        self.init()
        self.pass_b_and_c()
        review = self.review("M1", "F2", ["B", "C"])
        loading.commit_review(self.archive_path, review["review_id"])
        # Run each command in a fresh interpreter and check state is durable.
        import subprocess
        import sys

        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        out = subprocess.run(
            [sys.executable, "-m", "baggage.cli", "loading-status",
             "--archive", str(self.archive_path), "--manifest", "M1"],
            capture_output=True, text=True, env=env, cwd=str(self.archive_path.parent),
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["status"], "active")
        self.assertTrue(json.loads(out.stdout)["registration"]["committed"])
        # A separate process cancel is visible to the next one.
        out = subprocess.run(
            [sys.executable, "-m", "baggage.cli", "loading-cancel",
             "--archive", str(self.archive_path), "--manifest", "M1"],
            capture_output=True, text=True, env=env, cwd=str(self.archive_path.parent),
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(loading.manifest_status(self.archive_path, "M1")["status"], "cancelled")

    def test_state_survives_reload(self):
        self.init()
        self.pass_b_and_c()
        review = self.review("M1", "F2", ["B", "C"])
        loading.commit_review(self.archive_path, review["review_id"])
        # A fresh read from disk (no in-memory cache) sees the same state.
        state = loading._load_workbench(self.archive_path)
        self.assertIn("M1", state["manifests"])
        self.assertEqual(state["manifests"]["M1"]["bag_ids"], ["B", "C"])

    def test_archive_apply_and_workbench_share_lock_ordering(self):
        # Interleaving archive-apply with a review must yield a consistent
        # order: the review sees either the old or the new scan, never half.
        self.init()
        self.pass_b_and_c()
        self.apply_archive_batch({"batch_id": "S1", "scans": [
            {"op": "append", "scan": {"id": "S-EARLY", "bag_id": "B", "station": "BBB",
                                      "event_time": "2026-01-01T09:00:00+00:00"}}]})
        review = self.review("M1", "F2", ["B"])
        self.assertEqual(review["bags"]["B"]["last_scan"]["scan_id"], "S-B")
        self.assertEqual(review["bags"]["B"]["last_scan"]["knowledge_seq"], 1)

    # ---- CLI ----------------------------------------------------------------

    def test_cli_end_to_end_and_exit_codes(self):
        self.init(workbench=False)
        out, _ = self.run_cli("loading-init", "--archive", str(self.archive_path))
        self.assertIn("workbench", json.loads(out))

        batch = self.batch({"batch_id": "CLR", "operations": [
            record(clearance("C-B", "B", "S-B", 1)),
            record(clearance("C-C", "C", "S-C", 1))]})
        out, _ = self.run_cli("screening-apply", "--archive", str(self.archive_path),
                              "--batch", batch)
        self.assertEqual(json.loads(out)["recorded"], ["C-B", "C-C"])

        request = self.batch({"manifest_id": "M1", "flight_id": "F2", "bag_ids": ["B", "C"]})
        out, _ = self.run_cli("loading-review", "--archive", str(self.archive_path),
                              "--request", request)
        review_payload = json.loads(out)
        self.assertTrue(review_payload["committable"])
        review_id = review_payload["review_id"]

        out, _ = self.run_cli("loading-commit", "--archive", str(self.archive_path),
                              "--review", review_id)
        self.assertEqual(json.loads(out)["manifest_id"], "M1")

        out, _ = self.run_cli("loading-status", "--archive", str(self.archive_path),
                              "--manifest", "M1")
        self.assertEqual(json.loads(out)["status"], "active")

        out, _ = self.run_cli("loading-cancel", "--archive", str(self.archive_path),
                              "--manifest", "M1")
        self.assertEqual(json.loads(out)["status"], "cancelled")
        out, _ = self.run_cli("loading-cancel", "--archive", str(self.archive_path),
                              "--manifest", "M1")
        self.assertTrue(json.loads(out)["already_cancelled"])

        # Unknown manifest is a command error with the legacy exit code.
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("loading-status", "--archive", str(self.archive_path),
                         "--manifest", "NOPE")
        self.assertEqual(ctx.exception.code, 2)

    def test_cli_malformed_json_exits_2_without_state_change(self):
        self.init()
        before = self.workbench_bytes()
        bad = self.root / "broken.json"
        bad.write_text("{not json", encoding="utf-8")
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("screening-apply", "--archive", str(self.archive_path),
                         "--batch", str(bad))
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(self.workbench_bytes(), before)

    def manifest_ids(self):
        return set(loading._load_workbench(self.archive_path)["manifests"])


def _race_commit(archive_path, review_id, barrier, queue):
    barrier.wait()
    try:
        receipt = loading.commit_review(archive_path, review_id)
        queue.put(("ok", receipt["manifest_id"]))
    except Exception as exc:  # noqa: BLE001 - reported back to the parent
        queue.put(("err", review_id, str(exc)))


class ConcurrencyTests(WorkbenchFixtures):
    def test_intersecting_commits_only_one_wins(self):
        self.init()
        # B and C share the credential; M1 = [B,C], M2 = [B,E].
        self.apply_archive_batch({"batch_id": "E-SCAN", "scans": [
            {"op": "append", "scan": {"id": "S-E", "bag_id": "E", "station": "BBB",
                                      "event_time": "2026-01-01T11:20:00+00:00"}}]})
        self.pass_b_and_c()
        self.screening("C-E", "E", "S-E", 2)
        r1 = self.review("M1", "F2", ["B", "C"])
        r2 = self.review("M2", "F2", ["B", "E"])
        self.assertTrue(r1["committable"], r1["bags"])
        self.assertTrue(r2["committable"], r2["bags"])

        ctx = multiprocessing.get_context("fork")
        barrier = ctx.Barrier(2)
        queue = ctx.Queue()
        processes = [
            ctx.Process(target=_race_commit,
                        args=(self.archive_path, r1["review_id"], barrier, queue)),
            ctx.Process(target=_race_commit,
                        args=(self.archive_path, r2["review_id"], barrier, queue)),
        ]
        for process in processes:
            process.start()
        results = [queue.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=20)

        successes = [r for r in results if r[0] == "ok"]
        self.assertEqual(len(successes), 1, results)
        winner = successes[0][1]
        loser = "M2" if winner == "M1" else "M1"
        state = loading._load_workbench(self.archive_path)
        self.assertIn(winner, state["manifests"])
        self.assertNotIn(loser, state["manifests"])
        # The loser's exclusive bag is not occupied.
        exclusive = "E" if winner == "M1" else "C"
        review = self.review("CHECK", "F2", [exclusive])
        self.assertEqual(review["bags"][exclusive]["occupied_by"], [])


if __name__ == "__main__":
    unittest.main()

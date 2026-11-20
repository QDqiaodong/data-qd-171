import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from baggage import archive, study
from baggage.cli import main


def initial_state():
    return {
        "flights": [
            {"id": "F1", "origin": "AAA", "destination": "BBB",
             "departure": "2026-01-01T10:00:00+00:00",
             "arrival": "2026-01-01T11:00:00+00:00"},
            {"id": "F2", "origin": "BBB", "destination": "CCC",
             "departure": "2026-01-01T11:45:00+00:00",
             "arrival": "2026-01-01T12:30:00+00:00"},
        ],
        "bags": [
            {"id": "A", "flight_id": "F1", "next_flight_id": "F2",
             "minimum_connection_minutes": 30},
            {"id": "B", "flight_id": "F1", "next_flight_id": "F2",
             "minimum_connection_minutes": 30},
            {"id": "C", "flight_id": "F1", "next_flight_id": "F2",
             "minimum_connection_minutes": 30},
            {"id": "D", "flight_id": "F1", "minimum_connection_minutes": 30},
        ],
        "scans": [
            {"id": "S-A", "bag_id": "A", "station": "BBB",
             "event_time": "2026-01-01T11:10:00+00:00"},
            {"id": "S-B", "bag_id": "B", "station": "BBB",
             "event_time": "2026-01-01T11:15:00+00:00"},
        ],
    }


def label(value, result, features_as_of=1):
    return {"bag_id": value, "result": result, "features_as_of": features_as_of}


def link(*bags):
    return {"op": "link", "bags": list(bags)}


def unlink(*bags):
    return {"op": "unlink", "bags": list(bags)}


class StudyFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive_path = self.root / "archive"
        self.counter = 0
        self.write_json("input.json", initial_state())
        archive.init_archive(str(self.root / "input.json"), self.archive_path)

    def write_json(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def batch_file(self, payload):
        self.counter += 1
        return self.write_json(f"batch-{self.counter}.json", payload)

    def request_file(self, payload):
        self.counter += 1
        return self.write_json(f"request-{self.counter}.json", payload)

    def init(self):
        return study.init_study(self.archive_path)

    def apply(self, **payload):
        payload.setdefault("batch_id", f"SB-{self.counter + 1}")
        return study.apply_study(self.archive_path, self.batch_file(payload))

    def publish(self, release_id, rows):
        return study.publish_release(
            self.archive_path, self.request_file({"release_id": release_id, "rows": rows})
        )

    def view(self, release=None, as_of=None):
        return study.view_study(self.archive_path, release, as_of)

    def workbench_bytes(self):
        return (self.archive_path / study.STUDY_FILE).read_bytes()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            main(list(argv))
        return out.getvalue().strip(), err.getvalue().strip()

    def rows(self, release="A-TRAIN-THEN-C-EVAL", as_of=None):
        view = self.view(release, as_of)
        return view, {
            row["bag_id"]: row
            for entry in view["releases"]
            for row in entry["availability"]["rows"]
        }


class StudyInitAndApplyCase(StudyFixtures):
    def test_init_is_unique_and_requires_archive(self):
        with self.assertRaisesRegex(ValueError, "not a bag-reconcile archive"):
            study.init_study(self.root / "missing")
        result = self.init()
        self.assertEqual(result["study_rev"], 0)
        self.assertTrue((self.archive_path / study.STUDY_FILE).is_file())
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.init()

    def test_commands_require_workbench(self):
        for call in (
            lambda: self.apply(labels=[label("A", "success")]),
            lambda: self.publish("R1", [{"bag_id": "A", "partition": "train"}]),
            lambda: self.view(),
        ):
            with self.assertRaisesRegex(ValueError, "workbench does not exist"):
                call()

    def test_label_batch_bumps_one_revision(self):
        self.init()
        result = self.apply(labels=[label("A", "success"), label("B", "failure")])
        self.assertEqual(result["study_rev"], 1)
        view = self.view()
        self.assertEqual(view["study_rev"], 1)
        self.assertEqual(view["releases"], [])
        # Every relabelling, even the same value, is a new label version.
        again = self.apply(labels=[label("A", "success")])
        self.assertEqual(again["study_rev"], 2)

    def test_link_batch_bumps_one_revision_and_unions(self):
        self.init()
        self.apply(labels=[label("A", "success")])
        self.apply(labels=[label("B", "success")])
        result = self.apply(links=[link("A", "B")])
        self.assertEqual(result["study_rev"], 3)
        # The pair is undirected: re-adding in reverse is a duplicate relation.
        with self.assertRaisesRegex(ValueError, "already linked"):
            self.apply(links=[link("B", "A")])
        with self.assertRaisesRegex(ValueError, "more than once"):
            self.apply(batch_id="DUP-PAIR", links=[unlink("A", "B"), link("A", "B")])
        with self.assertRaisesRegex(ValueError, "not linked"):
            self.apply(links=[unlink("A", "C")])
        with self.assertRaisesRegex(ValueError, "different bags"):
            self.apply(links=[link("A", "A")])
        with self.assertRaisesRegex(ValueError, "unknown bag"):
            self.apply(links=[link("A", "ZZ")])

    def test_label_validation(self):
        self.init()
        with self.assertRaisesRegex(ValueError, "unknown bag"):
            self.apply(labels=[label("ZZ", "success")])
        with self.assertRaisesRegex(ValueError, "success"):
            self.apply(labels=[label("A", "unsure")])
        with self.assertRaisesRegex(ValueError, "accepted knowledge_seq"):
            self.apply(labels=[label("A", "success", features_as_of=99)])
        with self.assertRaisesRegex(ValueError, "accepted knowledge_seq"):
            self.apply(labels=[label("A", "success", features_as_of=0)])
        with self.assertRaisesRegex(ValueError, "has no valid label"):
            self.apply(labels=[{"bag_id": "A", "result": None}])
        with self.assertRaisesRegex(ValueError, "more than one label"):
            self.apply(labels=[label("A", "success"), label("A", "failure")])

    def test_failed_batch_is_all_or_nothing(self):
        self.init()
        self.apply(labels=[label("A", "success")])
        before = self.workbench_bytes()
        with self.assertRaisesRegex(ValueError, "unknown bag"):
            self.apply(labels=[label("C", "success")], links=[link("B", "ZZ")])
        self.assertEqual(self.workbench_bytes(), before)
        self.assertEqual(self.view()["study_rev"], 1)
        # The valid half did not apply: reusing its batch id is still new.
        with self.assertRaisesRegex(ValueError, "has no valid label"):
            self.publish("R", [{"bag_id": "C", "partition": "train"}])

    def test_duplicate_batch_id_is_rejected(self):
        self.init()
        self.apply(batch_id="B1", labels=[label("A", "success")])
        with self.assertRaisesRegex(ValueError, "duplicate study batch_id"):
            self.apply(batch_id="B1", labels=[label("B", "success")])

    def test_retract_then_relabel_keeps_identity(self):
        self.init()
        self.apply(labels=[label("A", "success")])
        self.apply(labels=[{"bag_id": "A", "result": None}])
        # No release exists yet, so publish is blocked until a live label returns.
        with self.assertRaisesRegex(ValueError, "has no valid label"):
            self.publish("R", [{"bag_id": "A", "partition": "train"}])
        self.apply(labels=[label("A", "failure", features_as_of=1)])
        published = self.publish("R", [{"bag_id": "A", "partition": "train"}])
        self.assertEqual(published["rows"][0]["label"]["result"], "failure")

    def test_duplicate_json_keys_and_types_are_rejected(self):
        self.init()
        path = self.root / "dup.json"
        path.write_text(
            '{"batch_id":"D","labels":[{"bag_id":"A","result":"success",'
            '"features_as_of":1,"features_as_of":1}]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            study.apply_study(self.archive_path, path)
        with self.assertRaisesRegex(ValueError, "labels and links must be lists"):
            self.apply(labels={})
        with self.assertRaisesRegex(ValueError, "at least one"):
            self.apply(batch_id="EMPTY")
        with self.assertRaisesRegex(ValueError, "non-empty string"):
            self.apply(labels=[{"bag_id": "", "result": "success", "features_as_of": 1}])


class StudyPublishCase(StudyFixtures):
    def setUp(self):
        super().setUp()
        self.init()
        self.apply(labels=[label("A", "success"), label("C", "failure"),
                           label("D", "success")])

    def test_publish_freezes_label_and_archive_features(self):
        release = self.publish(
            "R1",
            [{"bag_id": "D", "partition": "eval"}, {"bag_id": "A", "partition": "train"}],
        )
        self.assertEqual(release["study_rev"], 2)
        self.assertEqual([row["bag_id"] for row in release["rows"]], ["A", "D"])
        row_a = release["rows"][0]
        self.assertEqual(row_a["label"]["result"], "success")
        self.assertEqual(row_a["label"]["features_as_of"], 1)
        self.assertEqual(row_a["label"]["batch_id"], self.view()["releases"][0]["frozen"]["rows"][0]["label"]["batch_id"])
        self.assertEqual(row_a["features"]["status"], "arrived")
        self.assertEqual(row_a["features"]["last_scan"], "S-A")
        self.assertFalse(row_a["features"]["connection_at_risk"])
        self.assertEqual(row_a["sources"]["last_scan"],
                         {"scan_id": "S-A", "knowledge_seq": 1, "batch_id": None})
        self.assertEqual(row_a["sources"]["flights"]["current"],
                         {"flight_id": "F1", "knowledge_seq": 1, "batch_id": None})
        self.assertEqual(row_a["sources"]["flights"]["next"],
                         {"flight_id": "F2", "knowledge_seq": 1, "batch_id": None})
        self.assertIsNone(release["rows"][1]["sources"]["flights"]["next"])

    def test_publish_requires_live_labels_and_known_bags(self):
        with self.assertRaisesRegex(ValueError, "has no valid label"):
            self.publish("R1", [{"bag_id": "B", "partition": "train"}])
        with self.assertRaisesRegex(ValueError, "unknown bag"):
            self.publish("R1", [{"bag_id": "ZZ", "partition": "train"}])
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            self.publish("R1", [])
        with self.assertRaisesRegex(ValueError, "repeats bag"):
            self.publish("R1", [{"bag_id": "A", "partition": "train"},
                                {"bag_id": "A", "partition": "eval"}])
        with self.assertRaisesRegex(ValueError, "partition must be"):
            self.publish("R1", [{"bag_id": "A", "partition": "test"}])
        # Nothing was published or consumed.
        self.assertEqual(self.view()["study_rev"], 1)

    def test_publish_is_all_or_nothing_across_a_group(self):
        # A-B linked means both are one anomaly group; splitting it is refused.
        self.apply(links=[link("A", "B")])
        self.apply(labels=[label("B", "success")])
        before = self.workbench_bytes()
        with self.assertRaisesRegex(ValueError, "cannot be sent to both"):
            self.publish("R1", [{"bag_id": "A", "partition": "train"},
                                {"bag_id": "B", "partition": "eval"}])
        self.assertEqual(self.workbench_bytes(), before)
        # The whole release is refused, not just the offending row.
        # The rejected request consumed nothing: an unconnected bag can still
        # be published to either partition afterwards.
        self.publish("R1", [{"bag_id": "A", "partition": "train"},
                            {"bag_id": "D", "partition": "eval"}])

    def test_group_already_exposed_blocks_other_partition(self):
        self.publish("R-A", [{"bag_id": "A", "partition": "train"}])
        self.apply(links=[link("A", "B")])
        self.apply(labels=[label("B", "success"), label("D", "success")])
        # B is now connected to A's train exposure, so B may only go to train.
        with self.assertRaisesRegex(ValueError, "already exposed to train"):
            self.publish("R-B", [{"bag_id": "B", "partition": "eval"}])
        self.publish("R-B", [{"bag_id": "B", "partition": "train"}])

    def test_retry_returns_first_release_even_if_now_withdrawn(self):
        first = self.publish("R1", [{"bag_id": "A", "partition": "train"}])
        self.publish("R2", [{"bag_id": "D", "partition": "eval"}])
        # A late link contaminates A's train release with D's eval exposure.
        self.apply(links=[link("A", "D")])
        _, rows = self.rows("R1")
        self.assertFalse(rows["A"]["usable"])
        # The same request still replays its original frozen release.
        again = self.publish("R1", [{"bag_id": "A", "partition": "train"}])
        self.assertEqual(again, first)
        self.assertEqual(self.view()["study_rev"], 4)

    def test_retry_returns_first_release_even_after_label_retraction(self):
        first = self.publish("R1", [{"bag_id": "A", "partition": "train"}])
        self.apply(labels=[{"bag_id": "A", "result": None}])
        again = self.publish("R1", [{"bag_id": "A", "partition": "train"}])
        self.assertEqual(again, first)
        # A different row (now impossible anyway) is still a content mismatch.
        with self.assertRaisesRegex(ValueError, "different content"):
            self.publish("R1", [{"bag_id": "A", "partition": "eval"}])

    def test_request_order_does_not_matter_but_content_must_match(self):
        first = self.publish("R1", [{"bag_id": "A", "partition": "train"},
                                    {"bag_id": "D", "partition": "eval"}])
        reordered = self.publish("R1", [{"bag_id": "D", "partition": "eval"},
                                        {"bag_id": "A", "partition": "train"}])
        self.assertEqual(reordered, first)
        with self.assertRaisesRegex(ValueError, "different content"):
            self.publish("R1", [{"bag_id": "A", "partition": "eval"}])

    def test_rejected_id_can_be_reused_after_correction(self):
        self.apply(links=[link("A", "B")])
        with self.assertRaisesRegex(ValueError, "has no valid label"):
            self.publish("R1", [{"bag_id": "B", "partition": "train"}])
        self.apply(labels=[label("B", "success")])
        published = self.publish("R1", [{"bag_id": "B", "partition": "train"}])
        self.assertEqual(published["release_id"], "R1")

    def test_frozen_features_use_each_labels_own_archive_cutoff(self):
        scan_batch = {
            "batch_id": "SCAN-2",
            "scans": [{"op": "replace", "scan_id": "S-A",
                       "scan": {"id": "S-A", "bag_id": "A", "station": "BBB",
                                "event_time": "2026-01-01T11:40:00+00:00"}}],
        }
        archive.apply_batch(self.archive_path, self.batch_file(scan_batch))
        flight_batch = {
            "batch_id": "FLIGHT-3",
            "flights": [{"flight_id": "F1", "origin": "AAA", "destination": "BBB",
                         "departure": "2026-01-01T09:30:00+00:00",
                         "arrival": "2026-01-01T10:55:00+00:00"}],
        }
        archive.apply_batch(self.archive_path, self.batch_file(flight_batch))
        # A's label stays pinned to sequence 1, D's to sequence 3, even though
        # the archive is now at 3 for both.
        self.apply(labels=[label("A", "success", features_as_of=1),
                           label("D", "success", features_as_of=3)])
        release = self.publish("R1", [{"bag_id": "A", "partition": "train"},
                                      {"bag_id": "D", "partition": "train"}])
        rows = {row["bag_id"]: row for row in release["rows"]}
        self.assertEqual(rows["A"]["sources"]["last_scan"]["knowledge_seq"], 1)
        self.assertFalse(rows["A"]["features"]["connection_at_risk"])
        self.assertEqual(rows["A"]["sources"]["flights"]["current"]["knowledge_seq"], 1)
        self.assertEqual(rows["D"]["sources"]["flights"]["current"]["knowledge_seq"], 3)
        self.assertEqual(rows["D"]["sources"]["flights"]["current"]["batch_id"], "FLIGHT-3")
        self.assertEqual(rows["D"]["sources"]["last_scan"], None)
        # The release as a whole records the archive cutoff it was made at,
        # without overriding the per-row frozen versions.
        self.assertEqual(release["archive_knowledge_seq"], 3)
        # A later archive batch cannot change the frozen row.
        archive.apply_batch(self.archive_path, self.batch_file({
            "batch_id": "FLIGHT-4",
            "flights": [{"flight_id": "F1", "origin": "AAA", "destination": "BBB",
                         "departure": "2026-01-01T09:00:00+00:00",
                         "arrival": "2026-01-01T10:30:00+00:00"}],
        }))
        self.assertEqual(
            self.view("R1")["releases"][0]["frozen"],
            release,
        )

    def test_uncommitted_labels_do_not_leak_into_frozen_row(self):
        # A later relabel must not change an already frozen release.
        first = self.publish("R1", [{"bag_id": "A", "partition": "train"}])
        self.apply(labels=[label("A", "failure", features_as_of=1)])
        view, rows = self.rows("R1")
        self.assertEqual(rows["A"]["label"]["frozen"]["result"], "success")
        self.assertEqual(rows["A"]["label"]["current"]["result"], "failure")
        self.assertFalse(rows["A"]["usable"])
        self.assertEqual(rows["A"]["withdrawn_reasons"], ["label_version_changed"])
        # The frozen release itself is byte-identical.
        self.assertEqual(view["releases"][0]["frozen"], first)


class StudyViewCase(StudyFixtures):
    def setUp(self):
        super().setUp()
        self.init()
        self.apply(labels=[label("A", "success"), label("C", "failure"),
                           label("D", "success")])
        self.publish("REV-OLD", [{"bag_id": "A", "partition": "train"}])
        self.publish("REV-OLD", [{"bag_id": "A", "partition": "train"}])

    def test_revision_zero_is_empty(self):
        view = self.view(as_of=0)
        self.assertEqual(view["study_rev"], 0)
        self.assertEqual(view["releases"], [])
        with self.assertRaisesRegex(ValueError, "did not exist"):
            self.view("REV-OLD", as_of=1)

    def test_unknown_release_and_revision(self):
        with self.assertRaisesRegex(ValueError, "unknown release"):
            self.view("NOPE")
        with self.assertRaisesRegex(ValueError, "unknown study revision"):
            self.view(as_of=99)
        # A release that did not exist yet at the requested revision errors.
        self.publish("REV-NEW", [{"bag_id": "D", "partition": "eval"}])
        with self.assertRaisesRegex(ValueError, "did not exist"):
            self.view("REV-NEW", as_of=2)

    def test_view_reports_revision_and_separates_frozen_from_availability(self):
        view = self.view("REV-OLD")
        self.assertEqual(view["study_rev"], view["current_study_rev"])
        self.assertEqual(view["releases"][0]["published_study_rev"], 2)
        availability = view["releases"][0]["availability"]
        self.assertEqual(availability["checked_at_study_rev"], view["study_rev"])
        self.assertIn("frozen", view["releases"][0])
        self.assertTrue(availability["rows"][0]["usable"])

    def test_view_all_releases_at_a_revision(self):
        self.apply(labels=[label("C", "success", features_as_of=1)])
        self.publish("REV-SECOND", [{"bag_id": "C", "partition": "eval"}])
        view = self.view(as_of=4)
        self.assertEqual([r["release_id"] for r in view["releases"]], ["REV-OLD", "REV-SECOND"])
        old_view = self.view(as_of=3)
        self.assertEqual([r["release_id"] for r in old_view["releases"]], ["REV-OLD"])


class StudyAcceptanceCase(StudyFixtures):
    """The mandated end-to-end contamination sequence."""

    def test_acceptance_sequence(self):
        self.init()
        # 1. Label A and C, publish A to train, C to eval.
        self.apply(labels=[label("A", "success"), label("C", "failure"),
                           label("D", "success")])
        self.publish("REL-A", [{"bag_id": "A", "partition": "train"}])
        self.publish("REL-C", [{"bag_id": "C", "partition": "eval"}])
        _, rows = self.rows("REL-A")
        self.assertTrue(rows["A"]["usable"])

        # 2. Late A-B and B-C links arrive; B has no label.
        self.apply(links=[link("A", "B"), link("B", "C")])
        view = self.view()
        by_release = {r["release_id"]: r for r in view["releases"]}
        for release_id in ("REL-A", "REL-C"):
            row = by_release[release_id]["availability"]["rows"][0]
            self.assertFalse(row["usable"])
            self.assertIn("group_exposure_conflict", row["withdrawn_reasons"])
            evidence = row["evidence"]
            self.assertEqual(evidence["group"], ["A", "B", "C"])
            self.assertEqual(set(evidence["exposures"]), {"train", "eval"})
            train = evidence["exposures"]["train"]["exposure"]
            ev = evidence["exposures"]["eval"]["exposure"]
            self.assertEqual((train["bag_id"], train["release_id"]), ("A", "REL-A"))
            self.assertEqual((ev["bag_id"], ev["release_id"]), ("C", "REL-C"))
            # Each side has a valid chain from the checked bag to the exposure;
            # a bag is trivially its own exposure (empty chain).
            for side in ("train", "eval"):
                chain = evidence["exposures"][side]["chain"]
                endpoint = evidence["exposures"][side]["exposure"]["bag_id"]
                if row["bag_id"] == endpoint:
                    self.assertEqual(chain, [])
                else:
                    self.assertTrue(chain)
                    nodes = [row["bag_id"]] + [n for edge in chain for n in edge["bags"]]
                    self.assertIn(endpoint, nodes)

        # Another labelled bag hanging off C cannot publish to either side.
        self.apply(links=[link("C", "D")])
        with self.assertRaisesRegex(ValueError, "both train and eval"):
            self.publish("REL-D", [{"bag_id": "D", "partition": "train"}])
        with self.assertRaisesRegex(ValueError, "both train and eval"):
            self.publish("REL-D", [{"bag_id": "D", "partition": "eval"}])

        # 3. Adding A-C and removing B-C must not clear contamination.
        self.apply(links=[link("A", "C")])
        self.apply(links=[unlink("B", "C")])
        view = self.view()
        for release_id in ("REL-A", "REL-C"):
            row = next(
                r["availability"]["rows"][0]
                for r in view["releases"] if r["release_id"] == release_id
            )
            self.assertFalse(row["usable"])
            self.assertIn("group_exposure_conflict", row["withdrawn_reasons"])

        # 4. Removing A-C clears the conflict; groups return to their partitions.
        self.apply(links=[unlink("A", "C")])
        view = self.view()
        rows = {
            r["release_id"]: r["availability"]["rows"][0]
            for r in view["releases"]
        }
        self.assertTrue(rows["REL-A"]["usable"])
        self.assertTrue(rows["REL-C"]["usable"])

        # 5. Retract and refill A's label: the old A row withdraws for the label
        #    version, but A's train exposure still bars its group from eval.
        self.apply(labels=[{"bag_id": "A", "result": None}])
        self.apply(labels=[label("A", "success", features_as_of=1)])
        _, rows = self.rows("REL-A")
        self.assertFalse(rows["A"]["usable"])
        self.assertEqual(rows["A"]["withdrawn_reasons"], ["label_version_changed"])
        self.assertEqual(rows["A"]["group"]["exposure_partitions"], ["train"])
        with self.assertRaisesRegex(ValueError, "already exposed to train"):
            self.publish("REL-A-EVAL", [{"bag_id": "A", "partition": "eval"}])
        # B (still linked to A) may only join the group's train exposure.
        self.apply(labels=[label("B", "success")])
        with self.assertRaisesRegex(ValueError, "already exposed to train"):
            self.publish("REL-B-EVAL", [{"bag_id": "B", "partition": "eval"}])
        self.publish("REL-B-TRAIN", [{"bag_id": "B", "partition": "train"}])

    def test_old_revision_queries_are_stable(self):
        self.init()
        self.apply(labels=[label("A", "success"), label("C", "failure"),
                           label("D", "success")])
        self.publish("REL-A", [{"bag_id": "A", "partition": "train"}])
        self.publish("REL-C", [{"bag_id": "C", "partition": "eval"}])
        rev_before_links = self.view()["study_rev"]
        snapshot = self.view(as_of=rev_before_links)
        self.apply(links=[link("A", "B"), link("B", "C")])
        # The earlier revision still sees the two independent groups.
        old = self.view(as_of=rev_before_links)
        self.assertEqual(old["releases"], snapshot["releases"])
        self.assertEqual(old["study_rev"], rev_before_links)
        for entry in old["releases"]:
            self.assertTrue(entry["availability"]["rows"][0]["usable"])
        # The current revision sees the contamination.
        now = self.view()
        for entry in now["releases"]:
            self.assertFalse(entry["availability"]["rows"][0]["usable"])

    def test_contamination_evidence_has_one_valid_chain_per_side(self):
        self.init()
        self.apply(labels=[label("A", "success"), label("C", "failure"),
                           label("D", "success")])
        self.publish("REL-A", [{"bag_id": "A", "partition": "train"}])
        self.publish("REL-C", [{"bag_id": "C", "partition": "eval"}])
        self.apply(links=[link("A", "D"), link("D", "C")])
        view = self.view("REL-A")
        row = view["releases"][0]["availability"]["rows"][0]
        self.assertFalse(row["usable"])
        chain = row["evidence"]["exposures"]["eval"]["chain"]
        self.assertEqual([edge["bags"] for edge in chain], [["A", "D"], ["D", "C"]])
        self.assertEqual(row["evidence"]["exposures"]["train"]["chain"], [])
        # Re-querying yields the same evidence.
        self.assertEqual(self.view("REL-A"), view)

    def test_label_change_and_contamination_both_reported(self):
        self.init()
        self.apply(labels=[label("A", "success"), label("C", "failure")])
        self.publish("REL-A", [{"bag_id": "A", "partition": "train"}])
        self.publish("REL-C", [{"bag_id": "C", "partition": "eval"}])
        self.apply(links=[link("A", "C")])
        self.apply(labels=[label("A", "failure")])
        view = self.view("REL-A")
        row = view["releases"][0]["availability"]["rows"][0]
        self.assertFalse(row["usable"])
        self.assertEqual(
            row["withdrawn_reasons"], ["label_version_changed", "group_exposure_conflict"]
        )


class StudyOrderAndAtomicityCase(StudyFixtures):
    def test_link_list_order_does_not_change_validity(self):
        self.init()
        self.apply(labels=[label("A", "success"), label("B", "success"),
                           label("C", "success")])
        self.apply(links=[link("A", "B"), link("B", "C")])
        self.assertEqual(self.view()["study_rev"], 2)
        # Same unordered set, reversed and with each pair reversed.
        self.apply(links=[unlink("C", "B"), unlink("B", "A")])
        self.assertEqual(self.view()["study_rev"], 3)
        self.apply(links=[link("C", "B"), link("B", "A")])
        self.assertEqual(self.view()["study_rev"], 4)

    def test_publish_row_order_does_not_change_the_outcome(self):
        self.init()
        self.apply(labels=[label("A", "success"), label("C", "failure")])
        self.apply(links=[link("A", "C")])
        rows = [{"bag_id": "A", "partition": "train"},
                {"bag_id": "C", "partition": "eval"}]
        for ordered in (rows, list(reversed(rows))):
            with self.assertRaisesRegex(ValueError, "cannot be sent to both"):
                self.publish("SPLIT", ordered)
        self.assertEqual(self.view()["study_rev"], 2)

    def test_failed_publish_writes_nothing_and_consumes_no_revision(self):
        self.init()
        self.apply(labels=[label("A", "success"), label("C", "failure")])
        self.publish("REL-A", [{"bag_id": "A", "partition": "train"}])
        before = (self.workbench_bytes(), self.view())
        self.apply(links=[link("A", "C")])
        after_link = (self.workbench_bytes(), self.view())
        with self.assertRaisesRegex(ValueError, "already exposed to train"):
            self.publish("REL-C", [{"bag_id": "C", "partition": "eval"}])
        self.assertEqual(self.workbench_bytes(), after_link[0])
        self.assertEqual(self.view(), after_link[1])
        self.assertNotEqual(before, after_link)

    def test_failed_apply_writes_nothing_and_consumes_no_revision(self):
        self.init()
        self.apply(labels=[label("A", "success")])
        before = self.workbench_bytes()
        with self.assertRaisesRegex(ValueError, "not linked"):
            self.apply(batch_id="FAIL-1", links=[unlink("A", "C")])
        self.assertEqual(self.workbench_bytes(), before)
        # The batch id was not consumed: it can be reused once corrected.
        self.apply(batch_id="FAIL-1", links=[link("A", "C")])

    def test_exposure_survives_label_retraction(self):
        self.init()
        self.apply(labels=[label("A", "success")])
        self.publish("REL-A", [{"bag_id": "A", "partition": "train"}])
        self.apply(labels=[{"bag_id": "A", "result": None}])
        _, rows = self.rows("REL-A")
        self.assertFalse(rows["A"]["usable"])
        self.assertEqual(rows["A"]["withdrawn_reasons"], ["label_version_changed"])
        # The train exposure is real and still recorded despite the retraction.
        self.assertEqual(rows["A"]["group"]["exposure_partitions"], ["train"])
        with self.assertRaisesRegex(ValueError, "has no valid label"):
            self.publish("REL-A2", [{"bag_id": "A", "partition": "train"}])

    def test_exposure_is_not_copied_to_unreleased_neighbours(self):
        self.init()
        self.apply(labels=[label("A", "success"), label("B", "success"),
                           label("C", "success"), label("D", "success")])
        self.publish("REL-A", [{"bag_id": "A", "partition": "train"}])
        # B, C, D are linked but were never themselves released.
        self.apply(links=[link("A", "B"), link("B", "C"), link("C", "D")])
        view = self.view()
        entry = next(r for r in view["releases"] if r["release_id"] == "REL-A")
        self.assertEqual(
            [(e["bag_id"], e["partition"]) for e in entry["availability"]["rows"][0]["group"]["exposures"]],
            [("A", "train")],
        )
        # The whole group is blocked from eval, but still allowed in train.
        with self.assertRaisesRegex(ValueError, "already exposed to train"):
            self.publish("REL-EVAL", [{"bag_id": "D", "partition": "eval"}])
        self.publish("REL-TRAIN", [{"bag_id": "D", "partition": "train"}])

    def test_fresh_group_may_choose_either_partition(self):
        self.init()
        self.apply(labels=[label("D", "success")])
        released = self.publish("REL-D", [{"bag_id": "D", "partition": "eval"}])
        self.assertEqual(released["rows"][0]["partition"], "eval")

    def test_link_chains_are_only_the_ones_valid_at_the_revision(self):
        self.init()
        self.apply(labels=[label("A", "success"), label("C", "failure"),
                           label("D", "success")])
        self.publish("REL-A", [{"bag_id": "A", "partition": "train"}])
        self.publish("REL-C", [{"bag_id": "C", "partition": "eval"}])
        self.apply(links=[link("A", "D")])
        partial_rev = self.view()["study_rev"]
        self.apply(links=[link("D", "C")])
        # At the partial revision A and C are still separate: nothing withdrew.
        old = self.view("REL-A", as_of=partial_rev)
        self.assertTrue(old["releases"][0]["availability"]["rows"][0]["usable"])
        # Only once the second link exists does the conflict appear.
        now = self.view("REL-A")
        row = now["releases"][0]["availability"]["rows"][0]
        self.assertFalse(row["usable"])
        self.assertEqual(
            [edge["bags"] for edge in row["evidence"]["exposures"]["eval"]["chain"]],
            [["A", "D"], ["D", "C"]],
        )

    def test_evidence_choice_is_stable_with_several_real_exposures(self):
        self.init()
        self.apply(labels=[label("A", "success"), label("B", "success"),
                           label("C", "failure"), label("D", "failure")])
        self.publish("T1", [{"bag_id": "A", "partition": "train"}])
        self.publish("T2", [{"bag_id": "B", "partition": "train"}])
        self.publish("E1", [{"bag_id": "C", "partition": "eval"}])
        self.publish("E2", [{"bag_id": "D", "partition": "eval"}])
        self.apply(links=[link("A", "C"), link("C", "B"), link("B", "D")])
        first = self.view("T2")
        row = first["releases"][0]["availability"]["rows"][0]
        self.assertFalse(row["usable"])
        # One deterministic example per side, and stable on repeat.
        self.assertEqual(
            (row["evidence"]["exposures"]["train"]["exposure"]["bag_id"],
             row["evidence"]["exposures"]["eval"]["exposure"]["bag_id"]),
            ("A", "C"),
        )
        self.assertEqual(self.view("T2"), first)

    def test_archive_and_views_are_read_only(self):
        self.init()
        events = self.archive_path / archive.EVENTS_FILE
        before = events.read_bytes()
        self.apply(labels=[label("A", "success"), label("C", "failure")])
        self.publish("R1", [{"bag_id": "A", "partition": "train"}])
        state_before = self.workbench_bytes()
        self.view()
        self.view(as_of=0)
        self.view(as_of=1)
        self.view("R1", as_of=2)
        self.assertEqual(self.workbench_bytes(), state_before)
        self.assertEqual(events.read_bytes(), before)

    def test_failed_write_leaves_no_partial_batch_or_revision(self):
        from unittest import mock

        self.init()
        self.apply(labels=[label("A", "success"), label("C", "failure")])
        snapshot = self.workbench_bytes()
        with mock.patch.object(study, "_save_study", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.apply(batch_id="LOST", labels=[label("D", "success")],
                           links=[link("A", "D")])
            with self.assertRaises(OSError):
                self.publish("REL-X", [{"bag_id": "A", "partition": "train"}])
        self.assertEqual(self.workbench_bytes(), snapshot)
        self.assertEqual(self.view()["study_rev"], 1)
        # Retrying after the failure succeeds once and consumes one revision.
        self.apply(batch_id="LOST", labels=[label("D", "success")], links=[link("A", "D")])
        self.assertEqual(self.view()["study_rev"], 2)
        self.assertNotIn("REL-X", [r["release_id"] for r in self.view()["releases"]])


class StudyCliCase(StudyFixtures):
    def test_cross_process_state_survives_restart(self):
        import os
        import subprocess
        import sys

        root = Path(__file__).resolve().parent.parent
        env = dict(os.environ, PYTHONPATH=str(root))

        def run(*argv):
            return subprocess.run(
                [sys.executable, "-c",
                 "import sys; from baggage.cli import main; main(sys.argv[1:])",
                 *argv],
                capture_output=True, text=True, env=env, check=False,
            )

        done = run("study-init", "--archive", str(self.archive_path))
        self.assertEqual(done.returncode, 0, done.stderr)
        batch = self.batch_file({"batch_id": "X1", "labels": [label("A", "success")]})
        done = run("study-apply", "--archive", str(self.archive_path), "--batch", batch)
        self.assertEqual(json.loads(done.stdout)["study_rev"], 1)
        request = self.request_file(
            {"release_id": "R1", "rows": [{"bag_id": "A", "partition": "train"}]}
        )
        done = run("study-publish", "--archive", str(self.archive_path),
                   "--request", request)
        self.assertEqual(json.loads(done.stdout)["release_id"], "R1")
        done = run("study-view", "--archive", str(self.archive_path), "--as-of", "2")
        view = json.loads(done.stdout)
        self.assertEqual(view["study_rev"], 2)
        self.assertTrue(view["releases"][0]["availability"]["rows"][0]["usable"])
        # A fresh process refuses an unknown revision with exit code 2.
        done = run("study-view", "--archive", str(self.archive_path), "--as-of", "9")
        self.assertEqual(done.returncode, 2)
        self.assertIn("bag-reconcile:", done.stdout)

    def test_cli_round_trip_and_exit_codes(self):
        out, err = self.run_cli("study-init", "--archive", str(self.archive_path))
        self.assertEqual(json.loads(out)["study_rev"], 0)
        batch = self.batch_file({"batch_id": "L1", "labels": [label("A", "success")]})
        out, _ = self.run_cli("study-apply", "--archive", str(self.archive_path),
                              "--batch", batch)
        self.assertEqual(json.loads(out)["study_rev"], 1)
        request = self.request_file(
            {"release_id": "R1", "rows": [{"bag_id": "A", "partition": "train"}]}
        )
        out, _ = self.run_cli("study-publish", "--archive", str(self.archive_path),
                              "--request", request)
        self.assertEqual(json.loads(out)["release_id"], "R1")
        out, _ = self.run_cli("study-view", "--archive", str(self.archive_path),
                              "--release", "R1", "--as-of", "2")
        view = json.loads(out)
        self.assertEqual(view["study_rev"], 2)
        self.assertTrue(view["releases"][0]["availability"]["rows"][0]["usable"])

        with self.assertRaises(SystemExit) as raised:
            self.run_cli("study-view", "--archive", str(self.archive_path),
                         "--as-of", "99")
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

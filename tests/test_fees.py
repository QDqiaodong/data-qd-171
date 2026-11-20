import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from baggage import archive, fees
from baggage.cli import main


def initial_state(f2_departure="2026-02-01T08:00:00+09:00"):
    """One transfer service B: F1 AAA->BBB then F2 BBB->CCC.

    F2 departs at 23:00Z on 2026-01-31, which is already February in the
    +09:00 local zone but still January in UTC.  The evidence window is
    [2026-01-01T10:00Z, 2026-01-31T22:00Z] inclusive.
    """
    return {
        "flights": [
            {"id": "F1", "origin": "AAA", "destination": "BBB",
             "departure": "2026-01-01T08:00:00+00:00",
             "arrival": "2026-01-01T10:00:00+00:00"},
            {"id": "F2", "origin": "BBB", "destination": "CCC",
             "departure": f2_departure,
             "arrival": "2026-02-01T01:00:00+00:00"},
        ],
        "bags": [
            {"id": "B", "flight_id": "F1", "next_flight_id": "F2",
             "minimum_connection_minutes": 60},
            {"id": "D", "flight_id": "F1"},
        ],
        "scans": [
            # Latest instant inside the inclusive window (window end).
            {"id": "S-BBB", "bag_id": "B", "station": "BBB",
             "event_time": "2026-01-31T22:00:00+00:00"},
        ],
    }


RATES = {"BBB": 1000, "CCC": 2000}

# Move the same service to station CCC in UTC month 2026-02.
REATTRIBUTE = {
    "batch_id": "MOVE-1",
    "flights": [
        {"flight_id": "F1", "origin": "AAA", "destination": "CCC",
         "departure": "2026-02-04T08:00:00+00:00",
         "arrival": "2026-02-04T10:00:00+00:00"},
        {"flight_id": "F2", "origin": "CCC", "destination": "DDD",
         "departure": "2026-02-05T12:00:00+00:00",
         "arrival": "2026-02-05T14:00:00+00:00"},
    ],
}
CCC_SCAN = {
    "batch_id": "MOVE-2",
    "scans": [
        {"op": "append", "scan": {"id": "S-CCC", "bag_id": "B", "station": "CCC",
                                  "event_time": "2026-02-05T11:00:00+00:00"}},
    ],
}


class FeeFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive_path = self.root / "archive"
        self.counter = 0
        self.initial = initial_state()

    # ---- fixtures -----------------------------------------------------------

    def init(self, state=None, rates=None):
        state = self.initial if state is None else state
        self.write_json("input.json", state)
        archive.init_archive(str(self.root / "input.json"), self.archive_path)
        rates_path = self.write_json("rates.json", RATES if rates is None else rates)
        fees.init_ledger(self.archive_path, rates_path)
        return self.archive_path

    def write_json(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def batch(self, payload, name=None):
        self.counter += 1
        return self.write_json(name or f"batch-{self.counter}.json", payload)

    def apply(self, payload):
        return archive.apply_batch(self.archive_path, self.batch(payload))[0]

    def move_to_ccc(self):
        seq = self.apply(REATTRIBUTE)
        return self.apply(CCC_SCAN)

    def ledger_bytes(self):
        return (self.archive_path / fees.LEDGER_FILE).read_bytes()

    def ledger(self):
        return fees._load_ledger(self.archive_path)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            main(list(argv))
        return out.getvalue().strip(), err.getvalue().strip()


class FeeInitCase(FeeFixtures):
    def test_init_requires_archive_and_is_unique(self):
        with self.assertRaisesRegex(ValueError, "not a bag-reconcile archive"):
            fees.init_ledger(self.archive_path, self.write_json("r.json", RATES))
        self.init()
        with self.assertRaisesRegex(ValueError, "already exists"):
            fees.init_ledger(self.archive_path, self.write_json("r.json", RATES))

    def test_rate_table_is_strict(self):
        self.write_json("input.json", self.initial)
        archive.init_archive(str(self.root / "input.json"), self.archive_path)
        for bad in ([], {"BBB": 0}, {"BBB": -1}, {"BBB": 1.5},
                    {"BBB": True}, {"BBB": "1000"}, {"": 10}):
            path = self.write_json("bad.json", bad)
            with self.assertRaisesRegex(ValueError, "rate table"):
                fees.init_ledger(self.archive_path, path)
            self.assertFalse((self.archive_path / fees.LEDGER_FILE).exists())
        # Duplicate JSON keys are rejected by the strict loader.
        dup = self.root / "dup.json"
        dup.write_text('{"BBB": 1, "BBB": 2}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            fees.init_ledger(self.archive_path, str(dup))
        self.assertFalse((self.archive_path / fees.LEDGER_FILE).exists())

    def test_ledger_is_fixed_after_init(self):
        self.init()
        state = self.ledger()
        self.assertEqual(state["rates"], RATES)
        self.assertEqual(state["statements"], {})
        self.assertIsNone(state["last_processed_seq"])


class FeePreviewCase(FeeFixtures):
    def test_preview_shows_candidates_and_station_totals(self):
        self.init()
        result = fees.preview_month(self.archive_path, "2026-01", 1)
        self.assertTrue(result["recomputation"])
        self.assertEqual(result["accounting_effect"], "none")
        self.assertEqual(result["month_closed"], False)
        self.assertEqual([c["bag_id"] for c in result["candidates"]], ["B"])
        row = result["candidates"][0]
        self.assertTrue(row["billable"])
        self.assertEqual(row["reasons"], [])
        self.assertEqual(row["station"], "BBB")
        self.assertEqual(row["month"], "2026-01")
        self.assertEqual(row["amount_cents"], 1000)
        self.assertEqual(row["adopted_scan"]["scan_id"], "S-BBB")
        self.assertEqual(row["adopted_scan"]["knowledge_seq"], 1)
        self.assertIsNone(row["adopted_scan"]["batch_id"])
        self.assertIsNone(row["prior_flight"]["batch_id"])
        self.assertIsNone(row["next_flight"]["batch_id"])
        self.assertEqual(row["window_start"], "2026-01-01T10:00:00+00:00")
        self.assertEqual(row["window_end"], "2026-01-31T22:00:00+00:00")
        self.assertEqual(result["summary"]["billable_count"], 1)
        self.assertEqual(result["summary"]["amount_cents"], 1000)
        self.assertEqual(result["summary"]["by_station"],
                         [{"station": "BBB", "billable_count": 1,
                           "amount_cents": 1000}])
        # February has no candidate yet.
        other = fees.preview_month(self.archive_path, "2026-02", 1)
        self.assertEqual(other["candidates"], [])

    def test_preview_lists_non_billable_candidates(self):
        state = initial_state()
        state["bags"].append(
            {"id": "C", "flight_id": "F2", "next_flight_id": "F2",
             "minimum_connection_minutes": 30}
        )
        state["bags"].append(
            {"id": "E", "flight_id": "F1", "next_flight_id": "F2",
             "minimum_connection_minutes": -5}
        )
        self.init(state=state)
        result = fees.preview_month(self.archive_path, "2026-01", 1)
        by_bag = {c["bag_id"]: c for c in result["candidates"]}
        self.assertIn("C", by_bag)
        self.assertIn("same_leg", by_bag["C"]["reasons"])
        self.assertFalse(by_bag["C"]["billable"])
        self.assertIsNone(by_bag["C"]["amount_cents"])
        self.assertEqual(by_bag["E"]["reasons"],
                         ["invalid_minimum_connection_minutes"])
        self.assertEqual(result["summary"]["candidate_count"], 3)
        self.assertEqual(result["summary"]["billable_count"], 1)
        self.assertEqual(result["summary"]["non_billable_count"], 2)

    def test_preview_is_read_only_and_allows_older_cutoffs(self):
        self.init()
        fees.close_month(self.archive_path, "2026-01", 1)
        before = self.ledger_bytes()
        fees.preview_month(self.archive_path, "2026-01", 1)
        self.assertEqual(self.ledger_bytes(), before)

    def test_preview_rejects_unknown_sequence_and_bad_month(self):
        self.init()
        with self.assertRaisesRegex(ValueError, "unknown or unaccepted"):
            fees.preview_month(self.archive_path, "2026-01", 9)
        with self.assertRaisesRegex(ValueError, "invalid month"):
            fees.preview_month(self.archive_path, "2026-13", 1)

    def test_preview_chooses_earliest_qualifying_station_scan(self):
        state = initial_state()
        state["scans"] = [
            {"id": "S-BBB", "bag_id": "B", "station": "BBB",
             "event_time": "2026-01-31T22:00:00+00:00"},
            {"id": "S-BBB-LATE", "bag_id": "B", "station": "BBB",
             "event_time": "2026-01-15T10:00:00+00:00"},
            {"id": "S-ZZ", "bag_id": "B", "station": "BBB",
             "event_time": "2026-01-20T10:00:00+00:00"},
            {"id": "S-AA", "bag_id": "B", "station": "BBB",
             "event_time": "2026-01-20T10:00:00+00:00"},
            # A different station never supplies this station's evidence.
            {"id": "S-OTHER", "bag_id": "B", "station": "DDD",
             "event_time": "2026-01-02T00:00:00+00:00"},
        ]
        self.init(state=state)
        row = fees.preview_month(self.archive_path, "2026-01", 1)["candidates"][0]
        self.assertEqual(row["adopted_scan"]["scan_id"], "S-BBB-LATE")
        self.assertEqual(row["qualifying_scan_ids"],
                         ["S-BBB-LATE", "S-AA", "S-ZZ", "S-BBB"])

    def test_preview_scan_tie_breaks_on_scan_id(self):
        state = initial_state()
        state["scans"] = [
            {"id": "S-ZZ", "bag_id": "B", "station": "BBB",
             "event_time": "2026-01-15T10:00:00+00:00"},
            {"id": "S-AA", "bag_id": "B", "station": "BBB",
             "event_time": "2026-01-15T10:00:00+00:00"},
        ]
        self.init(state=state)
        row = fees.preview_month(self.archive_path, "2026-01", 1)["candidates"][0]
        self.assertEqual(row["adopted_scan"]["scan_id"], "S-AA")

    def test_preview_window_boundaries_are_inclusive(self):
        for event_time, expected in (
            ("2026-01-01T10:00:00+00:00", True),    # window start
            ("2026-01-31T22:00:00+00:00", True),    # window end
            ("2026-01-01T09:59:59+00:00", False),   # before
            ("2026-01-31T22:00:01+00:00", False),   # after
        ):
            with self.subTest(event_time=event_time):
                self.tmp.cleanup()
                self.tmp = tempfile.TemporaryDirectory()
                self.addCleanup(self.tmp.cleanup)
                self.root = Path(self.tmp.name)
                self.archive_path = self.root / "archive"
                state = initial_state()
                state["scans"] = [{"id": "S-BBB", "bag_id": "B", "station": "BBB",
                                   "event_time": event_time}]
                self.init(state=state)
                row = fees.preview_month(
                    self.archive_path, "2026-01", 1)["candidates"][0]
                self.assertIs(row["billable"], expected)
                if not expected:
                    self.assertIn("no_station_evidence", row["reasons"])

    def test_empty_window_is_not_billable(self):
        state = initial_state()
        state["bags"][0]["minimum_connection_minutes"] = 50000  # end before start
        self.init(state=state)
        row = fees.preview_month(self.archive_path, "2026-01", 1)["candidates"][0]
        self.assertFalse(row["billable"])
        self.assertIn("empty_evidence_window", row["reasons"])
        self.assertIsNone(row["adopted_scan"])

    def test_station_without_rate_is_not_billable(self):
        self.init(rates={"CCC": 2000})
        row = fees.preview_month(self.archive_path, "2026-01", 1)["candidates"][0]
        self.assertFalse(row["billable"])
        self.assertIn("no_rate_for_station", row["reasons"])


class FeeCloseCase(FeeFixtures):
    def test_first_close_creates_immutable_statement(self):
        self.init()
        result = fees.close_month(self.archive_path, "2026-01", 1)
        self.assertTrue(result["closed"])
        self.assertEqual(result["statement"]["total_amount_cents"], 1000)
        self.assertEqual(result["statement"]["line_count"], 1)
        self.assertIsNone(result["corrections"])
        state = self.ledger()
        statement = state["statements"]["2026-01"]
        line = statement["lines"][0]
        self.assertEqual(line["station"], "BBB")
        self.assertEqual(line["amount_cents"], 1000)
        self.assertEqual(line["basis"]["evidence"]["adopted_scan"]["scan_id"], "S-BBB")
        self.assertEqual(state["last_processed_seq"], 1)
        # Re-closing is refused and leaves the original untouched.
        before = self.ledger_bytes()
        with self.assertRaisesRegex(ValueError, "already closed"):
            fees.close_month(self.archive_path, "2026-01", 1)
        self.assertEqual(self.ledger_bytes(), before)

    def test_empty_month_is_still_registered_closed(self):
        self.init()
        result = fees.close_month(self.archive_path, "2026-02", 1)
        self.assertEqual(result["statement"]["total_amount_cents"], 0)
        self.assertEqual(result["statement"]["line_count"], 0)
        shown = fees.show_month(self.archive_path, "2026-02")
        self.assertEqual(shown["original_statement"]["lines"], [])
        self.assertEqual(shown["balance"]["amount_cents"], 0)

    def test_months_close_out_of_order_at_same_cutoff(self):
        self.init()
        fees.close_month(self.archive_path, "2026-03", 1)
        fees.close_month(self.archive_path, "2026-01", 1)
        self.assertEqual(sorted(self.ledger()["statements"]), ["2026-01", "2026-03"])

    def test_close_requires_accepted_cutoff_and_monotonic_order(self):
        self.init()
        with self.assertRaisesRegex(ValueError, "unknown or unaccepted"):
            fees.close_month(self.archive_path, "2026-01", 5)
        seq = self.apply({"batch_id": "TOUCH-1", "scans": [
            {"op": "append", "scan": {"id": "S-TOUCH", "bag_id": "D",
                                      "station": "AAA",
                                      "event_time": "2026-01-01T09:00:00+00:00"}}]})
        fees.close_month(self.archive_path, "2026-01", seq)
        # Stale cutoff cannot write.
        with self.assertRaisesRegex(ValueError, "older than the last processed"):
            fees.close_month(self.archive_path, "2026-02", 1)
        self.assertEqual(self.ledger()["last_processed_seq"], seq)


class FeeReattributionCase(FeeFixtures):
    def _close_january(self):
        self.init()
        fees.close_month(self.archive_path, "2026-01", 1)

    def test_first_close_settles_old_month_and_new_charge_atomically(self):
        self.init()
        fees.close_month(self.archive_path, "2026-01", 1)
        seq = self.move_to_ccc()
        # No separate fee-correct: the first close of February must both reverse
        # the January charge and add the February one in a single operation.
        result = fees.close_month(self.archive_path, "2026-02", seq)
        self.assertEqual(result["statement"]["total_amount_cents"], 2000)
        self.assertEqual(result["corrections"]["reversed_amount_cents"], -1000)
        self.assertEqual(result["corrections"]["charged_amount_cents"], 0)
        self.assertEqual(fees.show_month(self.archive_path, "2026-01")
                         ["balance"]["amount_cents"], 0)
        self.assertEqual(fees.show_month(self.archive_path, "2026-02")
                         ["balance"]["amount_cents"], 2000)

    def test_move_into_open_month_reverses_then_first_close_charges_once(self):
        self._close_january()
        cutoff = self.move_to_ccc()

        # Preview at the new cutoff shows the CCC February basis.
        preview = fees.preview_month(self.archive_path, "2026-02", cutoff)
        self.assertEqual(preview["month"], "2026-02")
        self.assertEqual(preview["summary"]["amount_cents"], 2000)
        self.assertEqual(preview["candidates"][0]["station"], "CCC")

        correction = fees.correct_ledger(self.archive_path, cutoff)
        self.assertEqual(correction["reversed_amount_cents"], -1000)
        self.assertEqual(correction["charged_amount_cents"], 0)
        self.assertEqual(correction["net_amount_cents"], -1000)
        self.assertFalse(self.ledger()["statements"].get("2026-02"))

        # The target month is still open: the service is not yet re-charged.
        with self.assertRaisesRegex(ValueError, "not closed"):
            fees.show_month(self.archive_path, "2026-02")

        closed = fees.close_month(self.archive_path, "2026-02", cutoff)
        self.assertEqual(closed["statement"]["total_amount_cents"], 2000)
        self.assertEqual(closed["statement"]["line_count"], 1)

        jan = fees.show_month(self.archive_path, "2026-01")
        self.assertEqual(jan["balance"]["amount_cents"], 0)
        feb = fees.show_month(self.archive_path, "2026-02")
        self.assertEqual(feb["balance"]["amount_cents"], 2000)

    def test_move_into_already_closed_month_applies_both_lines(self):
        self._close_january()
        fees.close_month(self.archive_path, "2026-02", 1)  # pre-closed, empty
        cutoff = self.move_to_ccc()
        correction = fees.correct_ledger(self.archive_path, cutoff)
        self.assertEqual(correction["reversed_amount_cents"], -1000)
        self.assertEqual(correction["charged_amount_cents"], 2000)
        self.assertEqual(correction["net_amount_cents"], 1000)
        self.assertEqual(correction["corrections"]["line_count"], 2)

        feb = fees.show_month(self.archive_path, "2026-02")
        self.assertEqual(feb["original_statement"]["lines"], [])
        self.assertEqual(len(feb["corrections"]), 1)
        line = feb["corrections"][0]["lines"][0]
        self.assertEqual(line["kind"], "charge")
        self.assertEqual(line["amount_cents"], 2000)
        self.assertEqual(line["station"], "CCC")
        self.assertEqual(feb["balance"]["amount_cents"], 2000)

        jan = fees.show_month(self.archive_path, "2026-01")
        reversal = jan["corrections"][0]["lines"][0]
        self.assertEqual(reversal["kind"], "reversal")
        self.assertEqual(reversal["amount_cents"], -1000)
        self.assertEqual(reversal["reversed_charge"]["amount_cents"], 1000)
        self.assertEqual(reversal["reason"], "reattributed")
        self.assertEqual(jan["balance"]["amount_cents"], 0)

    def test_close_with_no_new_fees_still_applies_old_month_correction(self):
        self._close_january()
        fees.close_month(self.archive_path, "2026-02", 1)
        cutoff = self.move_to_ccc()
        # Closing an unrelated empty month must still settle the old month.
        fees.close_month(self.archive_path, "2026-03", cutoff)
        jan = fees.show_month(self.archive_path, "2026-01")
        self.assertEqual(jan["balance"]["amount_cents"], 0)
        feb = fees.show_month(self.archive_path, "2026-02")
        self.assertEqual(feb["balance"]["amount_cents"], 2000)
        mar = fees.show_month(self.archive_path, "2026-03")
        self.assertEqual(mar["balance"]["amount_cents"], 0)

    def test_evidence_version_change_without_financial_change_adds_nothing(self):
        self.init()
        fees.close_month(self.archive_path, "2026-01", 1)
        # Replace with an identical logical scan: a new source version only.
        seq = self.apply({
            "batch_id": "TOUCH-2",
            "scans": [{"op": "replace", "scan_id": "S-BBB",
                       "scan": {"id": "S-BBB", "bag_id": "B", "station": "BBB",
                                "event_time": "2026-01-31T22:00:00+00:00"}}],
        })
        preview = fees.preview_month(self.archive_path, "2026-01", seq)
        self.assertEqual(preview["candidates"][0]["adopted_scan"]["knowledge_seq"], seq)
        correction = fees.correct_ledger(self.archive_path, seq)
        self.assertIsNone(correction["corrections"])
        self.assertEqual(correction["lines_added"], 0)
        # Only the audited cutoff advances; no ledger line is added.
        state = self.ledger()
        self.assertEqual(state["last_processed_seq"], seq)
        self.assertEqual(sum(len(st["lines"]) for st in state["statements"].values()), 1)

    def test_withdrawn_evidence_reverses_without_rewriting_statement(self):
        self.init()
        fees.close_month(self.archive_path, "2026-01", 1)
        original = fees.show_month(self.archive_path, "2026-01")["original_statement"]
        before_statement = json.loads(json.dumps(original))
        seq = self.apply({"batch_id": "RETRACT-1",
                          "scans": [{"op": "retract", "scan_id": "S-BBB"}]})
        correction = fees.correct_ledger(self.archive_path, seq)
        self.assertEqual(correction["reversed_amount_cents"], -1000)
        shown = fees.show_month(self.archive_path, "2026-01")
        self.assertEqual(shown["original_statement"], before_statement)
        self.assertEqual(shown["balance"]["amount_cents"], 0)
        self.assertEqual(shown["balance"]["by_station"][0]["balance_amount_cents"], 0)
        line = shown["corrections"][0]["lines"][0]
        self.assertEqual(line["reason"], "not_billable")
        self.assertIn("no_station_evidence", line["detail"]["unmet_reasons"])

    def test_late_evidence_supplements_closed_month(self):
        state = initial_state()
        state["scans"] = []
        state["bags"][1] = {"id": "D", "flight_id": "F1"}
        self.init(state=state)
        fees.close_month(self.archive_path, "2026-01", 1)
        self.assertEqual(fees.show_month(self.archive_path, "2026-01")
                         ["balance"]["amount_cents"], 0)
        seq = self.apply(CCC_SCAN)  # any batch to advance, then a BBB scan
        seq = self.apply({"batch_id": "LATE-1", "scans": [
            {"op": "append", "scan": {"id": "S-LATE", "bag_id": "B",
                                      "station": "BBB",
                                      "event_time": "2026-01-20T00:00:00+00:00"}}]})
        correction = fees.correct_ledger(self.archive_path, seq)
        self.assertEqual(correction["charged_amount_cents"], 1000)
        shown = fees.show_month(self.archive_path, "2026-01")
        self.assertEqual(shown["balance"]["amount_cents"], 1000)
        self.assertEqual(shown["corrections"][0]["lines"][0]["reason"], "supplemental")

    def test_returning_service_gets_a_new_positive_line(self):
        self.init()
        fees.close_month(self.archive_path, "2026-01", 1)
        retracted = self.apply({"batch_id": "RETRACT-R",
                                "scans": [{"op": "retract", "scan_id": "S-BBB"}]})
        fees.correct_ledger(self.archive_path, retracted)
        self.assertEqual(fees.show_month(self.archive_path, "2026-01")
                         ["balance"]["amount_cents"], 0)
        # Evidence returns at a later cutoff; the service is billable again.
        back = self.apply({"batch_id": "RESTORE-R", "scans": [
            {"op": "append", "scan": {"id": "S-BBB2", "bag_id": "B",
                                      "station": "BBB",
                                      "event_time": "2026-01-10T00:00:00+00:00"}}]})
        correction = fees.correct_ledger(self.archive_path, back)
        self.assertEqual(correction["charged_amount_cents"], 1000)
        shown = fees.show_month(self.archive_path, "2026-01")
        charges = [c for corr in shown["corrections"] for c in corr["lines"]
                   if c["kind"] == "charge"]
        self.assertEqual(len(charges), 1)
        self.assertEqual(charges[0]["reason"], "returned")
        # The old reversal is untouched and still points at the original charge.
        reversals = [c for corr in shown["corrections"] for c in corr["lines"]
                     if c["kind"] == "reversal"]
        self.assertEqual(len(reversals), 1)
        self.assertEqual(reversals[0]["reversed_charge"]["amount_cents"], 1000)
        self.assertNotEqual(charges[0]["line_id"], reversals[0]["reverses_line_id"])
        self.assertEqual(shown["balance"]["amount_cents"], 1000)

    def test_service_that_moves_away_and_back_uses_a_new_positive_line(self):
        self._close_january()
        seq = self.move_to_ccc()
        fees.correct_ledger(self.archive_path, seq)
        fees.close_month(self.archive_path, "2026-02", seq)
        # Move back to BBB January basis (a fresh FRR version).
        seq = self.apply({
            "batch_id": "BACK-1",
            "flights": [
                {"flight_id": "F1", "origin": "AAA", "destination": "BBB",
                 "departure": "2026-01-01T08:00:00+00:00",
                 "arrival": "2026-01-01T10:00:00+00:00"},
                {"flight_id": "F2", "origin": "BBB", "destination": "CCC",
                 "departure": "2026-01-31T23:00:00+00:00",
                 "arrival": "2026-02-01T01:00:00+00:00"},
            ],
        })
        correction = fees.correct_ledger(self.archive_path, seq)
        # Reverses the February charge and records a fresh January charge.
        self.assertEqual(correction["reversed_amount_cents"], -2000)
        self.assertEqual(correction["charged_amount_cents"], 1000)
        jan = fees.show_month(self.archive_path, "2026-01")
        charges = [c for corr in jan["corrections"] for c in corr["lines"]
                   if c["kind"] == "charge"]
        self.assertEqual(len(charges), 1)
        self.assertEqual(charges[0]["amount_cents"], 1000)
        self.assertEqual(charges[0]["reason"], "reattribution")
        # The original reversal still stands; nothing was cancelled.
        reversals = [c for corr in jan["corrections"] for c in corr["lines"]
                     if c["kind"] == "reversal"]
        self.assertEqual(len(reversals), 1)
        self.assertEqual(jan["balance"]["amount_cents"], 1000)
        self.assertEqual(fees.show_month(self.archive_path, "2026-02")
                         ["balance"]["amount_cents"], 0)


class FeeCorrectionIdempotencyCase(FeeFixtures):
    def test_same_cutoff_correction_is_not_repeated(self):
        self.init()
        fees.close_month(self.archive_path, "2026-01", 1)
        seq = self.move_to_ccc()
        first = fees.correct_ledger(self.archive_path, seq)
        self.assertEqual(first["lines_added"], 1)
        snapshot = self.ledger_bytes()
        second = fees.correct_ledger(self.archive_path, seq)
        self.assertEqual(second["lines_added"], 0)
        self.assertIsNone(second["corrections"])
        self.assertEqual(self.ledger_bytes(), snapshot)

    def test_new_cutoff_without_change_is_still_recorded(self):
        self.init()
        fees.close_month(self.archive_path, "2026-01", 1)
        seq = self.apply({"batch_id": "UNRELATED", "flights": [
            {"flight_id": "F1", "origin": "AAA", "destination": "BBB",
             "departure": "2026-01-01T08:00:00+00:00",
             "arrival": "2026-01-01T10:00:00+00:00"}]})
        result = fees.correct_ledger(self.archive_path, seq)
        self.assertEqual(result["lines_added"], 0)
        self.assertEqual(self.ledger()["last_processed_seq"], seq)

    def test_correct_after_close_at_same_cutoff_adds_nothing(self):
        self.init()
        fees.close_month(self.archive_path, "2026-01", 1)
        seq = self.move_to_ccc()
        fees.close_month(self.archive_path, "2026-02", seq)
        snapshot = self.ledger_bytes()
        result = fees.correct_ledger(self.archive_path, seq)
        self.assertEqual(result["lines_added"], 0)
        self.assertIsNone(result["corrections"])
        self.assertEqual(self.ledger_bytes(), snapshot)

    def test_commands_require_fee_init(self):
        self.write_json("input.json", self.initial)
        archive.init_archive(str(self.root / "input.json"), self.archive_path)
        for call in (
            lambda: fees.preview_month(self.archive_path, "2026-01", 1),
            lambda: fees.close_month(self.archive_path, "2026-01", 1),
            lambda: fees.correct_ledger(self.archive_path, 1),
            lambda: fees.show_month(self.archive_path, "2026-01"),
        ):
            with self.assertRaisesRegex(ValueError, "fee ledger does not exist"):
                call()

    def test_correct_requires_a_closed_month(self):
        self.init()
        with self.assertRaisesRegex(ValueError, "at least one closed month"):
            fees.correct_ledger(self.archive_path, 1)

    def test_stale_cutoff_can_preview_but_not_post(self):
        self.init()
        fees.close_month(self.archive_path, "2026-01", 1)
        seq = self.move_to_ccc()
        fees.correct_ledger(self.archive_path, seq)  # advances the cutoff to seq
        # A strictly older cutoff still previews, but cannot post.
        preview = fees.preview_month(self.archive_path, "2026-02", 1)
        self.assertEqual(preview["summary"]["amount_cents"], 0)
        with self.assertRaisesRegex(ValueError, "older than the last processed"):
            fees.correct_ledger(self.archive_path, 1)
        with self.assertRaisesRegex(ValueError, "older than the last processed"):
            fees.close_month(self.archive_path, "2026-02", 1)

    def test_failed_write_leaves_no_partial_state(self):
        self.init()
        fees.close_month(self.archive_path, "2026-01", 1)
        seq = self.move_to_ccc()
        snapshot = self.ledger_bytes()
        with mock.patch.object(fees, "_save_ledger", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                fees.correct_ledger(self.archive_path, seq)
        self.assertEqual(self.ledger_bytes(), snapshot)
        # Retry through a fresh process (state reloaded from disk) posts once.
        result = fees.correct_ledger(self.archive_path, seq)
        self.assertEqual(result["lines_added"], 1)
        again = fees.correct_ledger(self.archive_path, seq)
        self.assertEqual(again["lines_added"], 0)
        self.assertEqual(fees.show_month(self.archive_path, "2026-01")
                         ["balance"]["amount_cents"], 0)

    def test_failed_close_does_not_truncate_or_advance(self):
        self.init()
        fees.close_month(self.archive_path, "2026-01", 1)
        seq = self.move_to_ccc()
        snapshot = self.ledger_bytes()
        with mock.patch.object(fees, "_save_ledger", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                fees.close_month(self.archive_path, "2026-02", seq)
        self.assertEqual(self.ledger_bytes(), snapshot)
        closed = fees.close_month(self.archive_path, "2026-02", seq)
        self.assertEqual(closed["statement"]["total_amount_cents"], 2000)
        # One reversal in January, one charge from the first close.
        self.assertEqual(fees.show_month(self.archive_path, "2026-01")
                         ["balance"]["amount_cents"], 0)
        self.assertEqual(fees.show_month(self.archive_path, "2026-02")
                         ["balance"]["amount_cents"], 2000)


class FeeShowCase(FeeFixtures):
    def test_show_rejects_unknown_and_unclosed_months(self):
        self.init()
        with self.assertRaisesRegex(ValueError, "not closed"):
            fees.show_month(self.archive_path, "2026-01")
        fees.close_month(self.archive_path, "2026-01", 1)
        with self.assertRaisesRegex(ValueError, "not closed"):
            fees.show_month(self.archive_path, "2026-05")

    def test_show_is_stable_after_new_archive_batches(self):
        self.init()
        fees.close_month(self.archive_path, "2026-01", 1)
        first = fees.show_month(self.archive_path, "2026-01")
        self.apply({"batch_id": "NOISE", "scans": [
            {"op": "append", "scan": {"id": "S-NOISE", "bag_id": "B",
                                      "station": "BBB",
                                      "event_time": "2027-01-01T00:00:00+00:00"}}]})
        second = fees.show_month(self.archive_path, "2026-01")
        self.assertEqual(first, second)


class FeeTimezoneCase(FeeFixtures):
    def test_utc_month_is_independent_of_machine_timezone(self):
        self.init()
        row = fees.preview_month(self.archive_path, "2026-01", 1)["candidates"][0]
        # Local wall clock is February, but the attribution month is UTC January.
        self.assertEqual(row["month"], "2026-01")
        old = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Pacific/Kiritimati"
            if hasattr(time, "tzset"):
                time.tzset()
            again = fees.preview_month(self.archive_path, "2026-01", 1)
            self.assertEqual(again, fees.preview_month(self.archive_path, "2026-01", 1))
            self.assertEqual(again["candidates"][0]["month"], "2026-01")
            self.assertEqual(again["summary"]["amount_cents"], 1000)
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            if hasattr(time, "tzset"):
                time.tzset()


class FeeCliCase(FeeFixtures):
    def test_cli_round_trip(self):
        self.init()
        out, err = self.run_cli("fee-preview", "--archive", str(self.archive_path),
                                "--month", "2026-01", "--as-of", "1")
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertEqual(payload["summary"]["amount_cents"], 1000)

        out, _ = self.run_cli("fee-close", "--archive", str(self.archive_path),
                              "--month", "2026-01", "--as-of", "1")
        self.assertTrue(json.loads(out)["closed"])

        out, _ = self.run_cli("fee-show", "--archive", str(self.archive_path),
                              "--month", "2026-01")
        self.assertEqual(json.loads(out)["balance"]["amount_cents"], 1000)

    def test_cli_error_exit_code(self):
        self.init()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["fee-show", "--archive", str(self.archive_path),
                      "--month", "2026-01"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("bag-reconcile:", out.getvalue())


class FeeEvidenceRulesCase(FeeFixtures):
    def _fresh(self):
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive_path = self.root / "archive"

    def _row(self, mutate):
        self._fresh()
        state = initial_state()
        mutate(state)
        self.init(state=state)
        return fees.preview_month(self.archive_path, "2026-01", 1)["candidates"][0]

    def test_leg_identity_and_station_chain_are_checked(self):
        def chain(state):
            state["flights"][0]["destination"] = "XXX"

        def same(state):
            state["bags"][0]["next_flight_id"] = "F1"

        def late(state):
            state["flights"][0]["arrival"] = "2026-02-02T00:00:00+00:00"

        self.assertIn("prior_arrival_station_mismatch", self._row(chain)["reasons"])
        self.assertIn("same_leg", self._row(same)["reasons"])
        self.assertIn("prior_arrival_after_next_departure", self._row(late)["reasons"])

    def test_non_integer_minimum_connection_is_not_billable(self):
        def mutate(state):
            state["bags"][0]["minimum_connection_minutes"] = 30.5

        row = self._row(mutate)
        self.assertFalse(row["billable"])
        self.assertIn("invalid_minimum_connection_minutes", row["reasons"])

    def test_missing_rate_on_attribution_station(self):
        state = initial_state()
        self.init(state=state, rates={"CCC": 2000})
        row = fees.preview_month(self.archive_path, "2026-01", 1)["candidates"][0]
        self.assertEqual(row["reasons"], ["no_rate_for_station"])

    def test_ledger_never_touches_archive_events(self):
        self.init()
        before = (self.archive_path / archive.EVENTS_FILE).read_bytes()
        fees.close_month(self.archive_path, "2026-01", 1)
        seq = self.move_to_ccc()
        fees.preview_month(self.archive_path, "2026-02", seq)
        fees.correct_ledger(self.archive_path, seq)
        fees.show_month(self.archive_path, "2026-01")
        after_events = (self.archive_path / archive.EVENTS_FILE).read_bytes()
        self.assertEqual(before, after_events[:len(before)])
        # Existing archive queries are unchanged by the fee ledger.
        report = archive.build_report(self.archive_path, 1)
        self.assertEqual(report["knowledge_seq"], 1)

    def test_scan_recorded_out_of_order_is_still_ordered_by_instant(self):
        state = initial_state()
        state["scans"] = [
            {"id": "S-BBB", "bag_id": "B", "station": "BBB",
             "event_time": "2026-01-31T22:00:00+00:00"},
            {"id": "S-EARLY", "bag_id": "B", "station": "BBB",
             "event_time": "2026-01-02T00:00:00+00:00"},
        ]
        self.init(state=state)
        row = fees.preview_month(self.archive_path, "2026-01", 1)["candidates"][0]
        self.assertEqual(row["adopted_scan"]["scan_id"], "S-EARLY")

    def test_scan_offsets_compare_as_instants(self):
        state = initial_state()
        # 2026-01-01T19:00+09:00 == 2026-01-01T10:00Z: the window start.
        state["scans"] = [{"id": "S-BBB", "bag_id": "B", "station": "BBB",
                           "event_time": "2026-01-01T19:00:00+09:00"}]
        self.init(state=state)
        row = fees.preview_month(self.archive_path, "2026-01", 1)["candidates"][0]
        self.assertTrue(row["billable"], row["reasons"])
        self.assertEqual(row["adopted_scan"]["event_time"],
                         "2026-01-01T19:00:00+09:00")


class FeeRestartCase(FeeFixtures):
    """Each operation is a separate process, as a local operator would run it."""

    def run_process(self, *argv):
        root = Path(__file__).resolve().parent.parent
        env = dict(os.environ)
        env["PYTHONPATH"] = str(root)
        return subprocess.run(
            [sys.executable, "-m", "baggage.cli", *argv],
            cwd=root, env=env, capture_output=True, text=True,
        )

    def test_separate_processes_continue_without_double_charging(self):
        self.init()
        closed = self.run_process("fee-close", "--archive", str(self.archive_path),
                                  "--month", "2026-01", "--as-of", "1")
        self.assertEqual(closed.returncode, 0, closed.stderr)
        seq = self.move_to_ccc()

        first = self.run_process("fee-correct", "--archive", str(self.archive_path),
                                 "--as-of", str(seq))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["lines_added"], 1)

        # A whole new process re-reads state and must not charge twice.
        again = self.run_process("fee-correct", "--archive", str(self.archive_path),
                                 "--as-of", str(seq))
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(json.loads(again.stdout)["lines_added"], 0)

        shown = self.run_process("fee-show", "--archive", str(self.archive_path),
                                 "--month", "2026-01")
        self.assertEqual(json.loads(shown.stdout)["balance"]["amount_cents"], 0)

        closed = self.run_process("fee-close", "--archive", str(self.archive_path),
                                  "--month", "2026-02", "--as-of", str(seq))
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertEqual(json.loads(closed.stdout)["statement"]["total_amount_cents"],
                         2000)
        shown = self.run_process("fee-show", "--archive", str(self.archive_path),
                                 "--month", "2026-02")
        self.assertEqual(json.loads(shown.stdout)["balance"]["amount_cents"], 2000)

    def test_process_level_failure_and_retry(self):
        self.init()
        self.run_process("fee-close", "--archive", str(self.archive_path),
                         "--month", "2026-01", "--as-of", "1")
        seq = self.move_to_ccc()
        # A crashed / killed writer is simulated by a process whose save fails.
        script = (
            "from baggage import fees\n"
            f"fees._save_ledger = lambda *a, **k: (_ for _ in ()).throw(OSError('boom'))\n"
            f"fees.correct_ledger({str(self.archive_path)!r}, {seq})\n"
        )
        root = Path(__file__).resolve().parent.parent
        env = dict(os.environ)
        env["PYTHONPATH"] = str(root)
        result = subprocess.run([sys.executable, "-c", script], cwd=root, env=env,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        # Nothing partial was written; a normal retry posts exactly once.
        retry = self.run_process("fee-correct", "--archive", str(self.archive_path),
                                 "--as-of", str(seq))
        self.assertEqual(json.loads(retry.stdout)["lines_added"], 1)
        again = self.run_process("fee-correct", "--archive", str(self.archive_path),
                                 "--as-of", str(seq))
        self.assertEqual(json.loads(again.stdout)["lines_added"], 0)


if __name__ == "__main__":
    unittest.main()

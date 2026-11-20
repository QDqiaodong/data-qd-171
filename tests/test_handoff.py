import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from baggage import archive, handoff
from baggage.cli import main

ROOT = Path(__file__).resolve().parents[1]


def initial_state():
    return {
        "flights": [
            {"id": "F1", "origin": "AAA", "destination": "BBB",
             "departure": "2026-01-01T10:00:00+00:00",
             "arrival": "2026-01-01T11:00:00+00:00"},
            {"id": "F2", "origin": "BBB", "destination": "CCC",
             "departure": "2026-01-01T12:00:00+00:00",
             "arrival": "2026-01-01T13:00:00+00:00"},
        ],
        "bags": [
            {"id": "B", "flight_id": "F1", "next_flight_id": "F2",
             "minimum_connection_minutes": 30},
        ],
        "scans": [
            {"id": "S-BBB", "bag_id": "B", "station": "BBB",
             "event_time": "2026-01-01T11:10:00+00:00"},
            {"id": "S-CCC", "bag_id": "B", "station": "CCC",
             "event_time": "2026-01-01T11:20:00+00:00"},
        ],
    }


def flight_correction(origin="CCC", origin_batch="FIX-1"):
    return {
        "batch_id": origin_batch,
        "flights": [
            {"flight_id": "F2", "origin": origin, "destination": "DDD",
             "departure": "2026-01-01T12:00:00+00:00",
             "arrival": "2026-01-01T13:00:00+00:00"},
        ],
    }


def canonical_digest(body):
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class HandoffFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive_path = self.root / "archive"
        self.counter = 0

    def write_json(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def init_archive(self, state=None):
        input_path = self.write_json("input.json", state or initial_state())
        archive.init_archive(str(input_path), self.archive_path)
        return self.archive_path

    def apply(self, payload, name=None):
        self.counter += 1
        path = self.write_json(name or f"batch-{self.counter}.json", payload)
        return archive.apply_batch(self.archive_path, path)

    def export(self, stream, as_of, name=None):
        self.counter += 1
        path = self.root / (name or f"packet-{self.counter}.json")
        handoff.export_packet(self.archive_path, stream, as_of, path)
        return path

    def read(self, path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def craft(self, template, **overrides):
        """Rebuild a packet with a recomputed integrity digest."""
        body = {key: value for key, value in template.items() if key != "integrity"}
        body.update(overrides)
        body["integrity"] = {"algorithm": "sha256", "value": canonical_digest(body)}
        return body

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            main(list(argv))
        return out.getvalue().strip()


class HandoffUnitCase(HandoffFixtures):
    # ---- registration -------------------------------------------------------

    def test_sender_registration_is_immutable_and_ids_never_reused(self):
        self.init_archive()
        result = handoff.open_stream(self.archive_path, "STREAM-BBB", "BBB")
        self.assertEqual(result, {"stream_id": "STREAM-BBB", "desk": "BBB",
                                  "confirmed_seq": 0, "packet_no": 0})
        with self.assertRaisesRegex(ValueError, "already registered"):
            handoff.open_stream(self.archive_path, "STREAM-BBB", "BBB")
        with self.assertRaisesRegex(ValueError, "already registered"):
            handoff.open_stream(self.archive_path, "STREAM-BBB", "CCC")
        # A different stream under the same archive is independent.
        handoff.open_stream(self.archive_path, "STREAM-CCC", "CCC")
        self.assertEqual(handoff.export_packet(
            self.archive_path, "STREAM-CCC", 1, self.root / "c1.json")["packet_no"], 1)

    def test_open_stream_requires_an_archive(self):
        with self.assertRaisesRegex(ValueError, "not a bag-reconcile archive"):
            handoff.open_stream(self.archive_path, "S", "BBB")

    # ---- packet contents ----------------------------------------------------

    def test_packet_carries_desk_scan_scope_and_sources(self):
        self.init_archive()
        handoff.open_stream(self.archive_path, "STREAM-BBB", "BBB")
        path = self.export("STREAM-BBB", 1)
        packet = self.read(path)
        self.assertEqual(packet["format"], handoff.PACKET_FORMAT)
        self.assertEqual(packet["stream_id"], "STREAM-BBB")
        self.assertEqual(packet["desk"], "BBB")
        self.assertEqual(packet["packet_no"], 1)
        self.assertEqual(packet["prev_packet_no"], 0)
        self.assertEqual((packet["from_seq"], packet["to_seq"]), (0, 1))
        self.assertEqual(packet["retractions"], [])
        self.assertEqual(packet["changes"], [{
            "bag_id": "B",
            "next_flight_id": "F2",
            "origin": "BBB",
            "destination": "CCC",
            "departure": "2026-01-01T12:00:00+00:00",
            "arrival": "2026-01-01T13:00:00+00:00",
            "flight_knowledge_seq": 1,
            "latest_scan": {"scan_id": "S-BBB",
                            "event_time": "2026-01-01T11:10:00+00:00",
                            "knowledge_seq": 1},
        }])
        # The later CCC scan and the prior leg F1 never leak into the packet.
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("S-CCC", text)
        self.assertNotIn("F1", text)
        self.assertNotIn("clearance", text)
        self.assertEqual(set(packet), handoff._PACKET_KEYS)

    def test_other_desk_first_packet_has_no_bag(self):
        self.init_archive()
        handoff.open_stream(self.archive_path, "STREAM-CCC", "CCC")
        packet = self.read(self.export("STREAM-CCC", 1))
        self.assertEqual(packet["changes"], [])
        self.assertEqual(packet["retractions"], [])

    def test_bag_without_next_flight_is_not_in_scope(self):
        state = initial_state()
        state["bags"].append({"id": "NO-NEXT", "flight_id": "F1",
                              "minimum_connection_minutes": 30})
        state["scans"].append({"id": "S-NO", "bag_id": "NO-NEXT", "station": "BBB",
                               "event_time": "2026-01-01T11:00:00+00:00"})
        self.init_archive(state)
        handoff.open_stream(self.archive_path, "STREAM-BBB", "BBB")
        packet = self.read(self.export("STREAM-BBB", 1))
        self.assertEqual([row["bag_id"] for row in packet["changes"]], ["B"])

    # ---- empty packets and cutoff rules ------------------------------------

    def test_empty_packet_confirms_cutoff(self):
        self.init_archive()
        handoff.open_stream(self.archive_path, "STREAM-BBB", "BBB")
        inbox = self.root / "inbox"
        handoff.create_inbox(inbox, "STREAM-BBB", "BBB")
        # Deliver and confirm the first packet so the stream is caught up to 1.
        first = handoff.import_packet(inbox, self.export("STREAM-BBB", 1))
        handoff.acknowledge(self.archive_path, "STREAM-BBB",
                            self.write_json("r1.json", first))
        # Sequence 2 only revises the prior leg F1, which is not delivered.
        self.apply({"batch_id": "PRIOR-LEG", "flights": [
            {"flight_id": "F1", "origin": "AAA", "destination": "BBB",
             "departure": "2026-01-01T10:05:00+00:00",
             "arrival": "2026-01-01T11:05:00+00:00"}]})
        empty_path = self.export("STREAM-BBB", 2)
        empty = self.read(empty_path)
        self.assertEqual((empty["changes"], empty["retractions"]), ([], []))
        self.assertEqual((empty["packet_no"], empty["prev_packet_no"]), (2, 1))
        self.assertEqual((empty["from_seq"], empty["to_seq"]), (1, 2))
        receipt = handoff.import_packet(inbox, empty_path)
        self.assertEqual(receipt["to_seq"], 2)
        handoff.acknowledge(self.archive_path, "STREAM-BBB",
                            self.write_json("r2.json", receipt))
        # A zero-change packet still proves the desk reached the cutoff.
        view = handoff.view_inbox(inbox)
        self.assertEqual((view["packet_no"], view["confirmed_seq"]), (2, 2))
        self.assertEqual([row["bag_id"] for row in view["rows"]], ["B"])

    def test_cutoff_must_be_accepted_and_advance(self):
        self.init_archive()
        handoff.open_stream(self.archive_path, "STREAM-BBB", "BBB")
        for bad in (0, 2):
            with self.assertRaisesRegex(ValueError, "unknown or unaccepted"):
                handoff.export_packet(self.archive_path, "STREAM-BBB", bad,
                                      self.root / f"bad-{bad}.json")
        self.assertFalse((self.root / "bad-2.json").exists())
        # Confirm cutoff 1, then 1 is no longer strictly greater.
        inbox = self.root / "inbox"
        handoff.create_inbox(inbox, "STREAM-BBB", "BBB")
        receipt = handoff.import_packet(
            inbox, self.export("STREAM-BBB", 1))
        handoff.acknowledge(self.archive_path, "STREAM-BBB",
                            self.write_json("r.json", receipt))
        with self.assertRaisesRegex(ValueError, "strictly greater"):
            handoff.export_packet(self.archive_path, "STREAM-BBB", 1,
                                  self.root / "again.json")

    # ---- one pending packet, re-export -------------------------------------

    def test_pending_packet_blocks_other_cutoffs_and_reexports_identically(self):
        self.init_archive()
        handoff.open_stream(self.archive_path, "STREAM-BBB", "BBB")
        first = self.export("STREAM-BBB", 1)
        self.apply(flight_correction())
        with self.assertRaisesRegex(ValueError, "awaiting acknowledgement"):
            handoff.export_packet(self.archive_path, "STREAM-BBB", 2,
                                  self.root / "jump.json")
        self.assertFalse((self.root / "jump.json").exists())
        # Same cutoff, new path: identical bytes.
        second = self.export("STREAM-BBB", 1, name="again.json")
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_output_is_never_overwritten(self):
        self.init_archive()
        handoff.open_stream(self.archive_path, "STREAM-BBB", "BBB")
        occupied = self.root / "occupied.json"
        occupied.write_text("keep me", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            handoff.export_packet(self.archive_path, "STREAM-BBB", 1, occupied)
        self.assertEqual(occupied.read_text(encoding="utf-8"), "keep me")
        # No phantom pending packet blocked the retry.
        packet = self.read(self.export("STREAM-BBB", 1))
        self.assertEqual(packet["packet_no"], 1)

    def test_failed_publish_leaves_no_pending_packet(self):
        self.init_archive()
        handoff.open_stream(self.archive_path, "STREAM-BBB", "BBB")
        blocker = self.root / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        bad_output = blocker / "packet.json"
        with self.assertRaises(OSError):
            handoff.export_packet(self.archive_path, "STREAM-BBB", 1, bad_output)
        self.assertFalse(bad_output.exists())
        # The failed export neither consumed the packet number nor confirmed a cutoff.
        packet = self.read(self.export("STREAM-BBB", 1))
        self.assertEqual((packet["packet_no"], packet["prev_packet_no"]), (1, 0))

    # ---- inbox registration -------------------------------------------------

    def test_empty_inbox_view_reports_zero(self):
        inbox = self.root / "inbox"
        handoff.create_inbox(inbox, "STREAM-BBB", "BBB")
        self.assertEqual(handoff.view_inbox(inbox), {
            "stream_id": "STREAM-BBB", "desk": "BBB",
            "packet_no": 0, "confirmed_seq": 0, "rows": []})
        with self.assertRaisesRegex(ValueError, "already exists"):
            handoff.create_inbox(inbox, "STREAM-BBB", "BBB")

    def test_inbox_requires_registration(self):
        with self.assertRaisesRegex(ValueError, "not a handoff inbox"):
            handoff.view_inbox(self.root / "missing")

    # ---- receiver validation ------------------------------------------------

    def _applied_inbox(self):
        self.init_archive()
        handoff.open_stream(self.archive_path, "STREAM-BBB", "BBB")
        inbox = self.root / "inbox"
        handoff.create_inbox(inbox, "STREAM-BBB", "BBB")
        first = self.export("STREAM-BBB", 1)
        receipt = handoff.import_packet(inbox, first)
        return inbox, self.read(first), receipt

    def test_wrong_stream_and_desk_rejected(self):
        inbox, packet, _ = self._applied_inbox()
        wrong_stream = self.craft(packet, stream_id="OTHER")
        with self.assertRaisesRegex(ValueError, "stream OTHER"):
            handoff.import_packet(inbox, self.write_json("ws.json", wrong_stream))
        handoff.open_stream(self.archive_path, "STREAM-CCC", "CCC")
        other = self.export("STREAM-CCC", 1)
        with self.assertRaisesRegex(ValueError, "stream STREAM-CCC"):
            handoff.import_packet(inbox, other)
        # Same stream id but a different desk is also refused.
        wrong_desk = self.craft(packet, packet_no=2, prev_packet_no=1,
                                from_seq=1, to_seq=2, desk="CCC",
                                changes=[], retractions=[])
        with self.assertRaisesRegex(ValueError, "desk CCC"):
            handoff.import_packet(inbox, self.write_json("wd.json", wrong_desk))

    def test_skipped_and_out_of_order_packets_rejected(self):
        self.init_archive()
        handoff.open_stream(self.archive_path, "STREAM-BBB", "BBB")
        inbox = self.root / "inbox"
        handoff.create_inbox(inbox, "STREAM-BBB", "BBB")
        packet = self.read(self.export("STREAM-BBB", 1))
        skip = self.craft(packet, packet_no=2, prev_packet_no=1,
                          from_seq=1, to_seq=2)
        with self.assertRaisesRegex(ValueError, "does not immediately follow"):
            handoff.import_packet(inbox, self.write_json("skip.json", skip))
        bad_prev = self.craft(packet, prev_packet_no=5)
        with self.assertRaisesRegex(ValueError, "prev_packet_no"):
            handoff.import_packet(inbox, self.write_json("prev.json", bad_prev))
        bad_base = self.craft(packet, from_seq=7, to_seq=8)
        with self.assertRaisesRegex(ValueError, "base cutoff"):
            handoff.import_packet(inbox, self.write_json("base.json", bad_base))
        # Nothing was applied.
        self.assertEqual(handoff.view_inbox(inbox)["packet_no"], 0)

    def test_same_number_with_different_content_rejected(self):
        inbox, packet, first_receipt = self._applied_inbox()
        mutated = self.craft(packet, to_seq=2)
        with self.assertRaisesRegex(ValueError, "different content"):
            handoff.import_packet(inbox, self.write_json("mut.json", mutated))
        # The retry rule is checked before the cutoff rule: this mutated packet
        # would also fail the base-cutoff check, yet the content conflict is
        # reported and the copy stays at the first receipt.
        view = handoff.view_inbox(inbox)
        self.assertEqual((view["packet_no"], view["confirmed_seq"]), (1, 1))
        self.assertEqual(view["rows"][0]["bag_id"], "B")

    def test_tampered_integrity_rejected(self):
        self.init_archive()
        handoff.open_stream(self.archive_path, "STREAM-BBB", "BBB")
        inbox = self.root / "inbox"
        handoff.create_inbox(inbox, "STREAM-BBB", "BBB")
        packet = self.read(self.export("STREAM-BBB", 1))
        packet["changes"][0]["destination"] = "TAMPERED"
        with self.assertRaisesRegex(ValueError, "integrity check failed"):
            handoff.import_packet(inbox, self.write_json("tampered.json", packet))
        self.assertEqual(handoff.view_inbox(inbox)["packet_no"], 0)

    def test_duplicate_bag_and_unknown_retraction_rejected(self):
        self.init_archive()
        handoff.open_stream(self.archive_path, "STREAM-BBB", "BBB")
        inbox = self.root / "inbox"
        handoff.create_inbox(inbox, "STREAM-BBB", "BBB")
        packet = self.read(self.export("STREAM-BBB", 1))
        duplicated = self.craft(packet, changes=packet["changes"] * 2)
        with self.assertRaisesRegex(ValueError, "more than once"):
            handoff.import_packet(inbox, self.write_json("dup.json", duplicated))
        bad_retract = self.craft(packet, changes=[], retractions=["B"])
        with self.assertRaisesRegex(ValueError, "unknown bag B"):
            handoff.import_packet(inbox, self.write_json("retract.json", bad_retract))

    def test_scan_retraction_updates_row_instead_of_retracting(self):
        state = initial_state()
        state["scans"] = [
            {"id": "S1", "bag_id": "B", "station": "BBB",
             "event_time": "2026-01-01T11:00:00+00:00"},
            {"id": "S2", "bag_id": "B", "station": "BBB",
             "event_time": "2026-01-01T11:10:00+00:00"},
            {"id": "S-CCC", "bag_id": "B", "station": "CCC",
             "event_time": "2026-01-01T11:20:00+00:00"},
        ]
        self.init_archive(state)
        handoff.open_stream(self.archive_path, "STREAM-BBB", "BBB")
        inbox = self.root / "inbox"
        handoff.create_inbox(inbox, "STREAM-BBB", "BBB")
        first = self.export("STREAM-BBB", 1)
        handoff.acknowledge(
            self.archive_path, "STREAM-BBB",
            self.write_json("r1.json", handoff.import_packet(inbox, first)))
        self.assertEqual(handoff.view_inbox(inbox)["rows"][0]["latest_scan"]["scan_id"], "S2")

        self.apply({"batch_id": "RETRACT-S2", "scans": [{"op": "retract", "scan_id": "S2"}]})
        second = self.export("STREAM-BBB", 2)
        packet = self.read(second)
        self.assertEqual(packet["retractions"], [])
        self.assertEqual(packet["changes"][0]["latest_scan"]["scan_id"], "S1")
        handoff.acknowledge(
            self.archive_path, "STREAM-BBB",
            self.write_json("r2.json", handoff.import_packet(inbox, second)))
        self.assertEqual(handoff.view_inbox(inbox)["rows"][0]["latest_scan"]["scan_id"], "S1")

        self.apply({"batch_id": "RETRACT-S1", "scans": [{"op": "retract", "scan_id": "S1"}]})
        third = self.export("STREAM-BBB", 3)
        packet = self.read(third)
        # Still in scope, so the row remains with a null scan; the later CCC
        # scan is never substituted.
        self.assertEqual(packet["retractions"], [])
        self.assertEqual(packet["changes"][0]["latest_scan"], None)
        handoff.import_packet(inbox, third)
        self.assertEqual(handoff.view_inbox(inbox)["rows"][0]["latest_scan"], None)

    # ---- sender acknowledgement --------------------------------------------

    def _sender_with_pending(self):
        self.init_archive()
        handoff.open_stream(self.archive_path, "STREAM-BBB", "BBB")
        inbox = self.root / "inbox"
        handoff.create_inbox(inbox, "STREAM-BBB", "BBB")
        packet_path = self.export("STREAM-BBB", 1)
        receipt = handoff.import_packet(inbox, packet_path)
        return inbox, packet_path, receipt

    def test_ack_rejects_wrong_stream_content_and_unsent(self):
        _, _, receipt = self._sender_with_pending()
        wrong_stream = dict(receipt, stream_id="OTHER")
        with self.assertRaisesRegex(ValueError, "not STREAM-BBB"):
            handoff.acknowledge(self.archive_path, "STREAM-BBB",
                                self.write_json("r1.json", wrong_stream))
        wrong_content = dict(receipt, packet_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "does not match pending"):
            handoff.acknowledge(self.archive_path, "STREAM-BBB",
                                self.write_json("r2.json", wrong_content))
        never_sent = dict(receipt, packet_no=9, to_seq=9)
        with self.assertRaisesRegex(ValueError, "does not match pending"):
            handoff.acknowledge(self.archive_path, "STREAM-BBB",
                                self.write_json("r3.json", never_sent))
        # The stream is still pending at the first packet.
        self.apply(flight_correction())
        with self.assertRaisesRegex(ValueError, "awaiting acknowledgement"):
            handoff.export_packet(self.archive_path, "STREAM-BBB", 2,
                                  self.root / "still.json")
        self.assertFalse((self.root / "still.json").exists())

    def test_confirmed_receipt_replay_does_not_roll_back_with_next_pending(self):
        _, _, receipt = self._sender_with_pending()
        handoff.acknowledge(self.archive_path, "STREAM-BBB",
                            self.write_json("r.json", receipt))
        self.apply(flight_correction())
        second = self.export("STREAM-BBB", 2)  # pending packet 2
        self.assertEqual(self.read(second)["packet_no"], 2)
        # Re-delivering packet 1's real receipt succeeds but changes nothing.
        result = handoff.acknowledge(self.archive_path, "STREAM-BBB",
                                     self.write_json("r-again.json", receipt))
        self.assertEqual(result["advanced"], False)
        self.assertEqual(result["confirmed_seq"], 1)
        self.assertTrue(result["pending"])
        still_pending = self.export("STREAM-BBB", 2, name="still.json")
        self.assertEqual(still_pending.read_bytes(), second.read_bytes())

    # ---- restart recovery ---------------------------------------------------

    def test_import_applied_receipt_lost_returns_original_after_reload(self):
        inbox, packet_path, receipt = self._sender_with_pending()
        # "Restart": every call re-reads from disk, and this simulates a lost
        # receipt by asking the inbox to re-import the same bytes.
        again = handoff.import_packet(inbox, packet_path)
        self.assertEqual(again, receipt)
        self.assertEqual(handoff.view_inbox(inbox)["packet_no"], 1)
        handoff.acknowledge(self.archive_path, "STREAM-BBB",
                            self.write_json("r.json", again))

    def test_import_failure_leaves_last_copy_readable(self):
        inbox, packet_path, receipt = self._sender_with_pending()
        bad = self.craft(self.read(packet_path), packet_no=2, prev_packet_no=1,
                         from_seq=1, to_seq=2, changes=[], retractions=["GHOST"])
        with self.assertRaisesRegex(ValueError, "unknown bag GHOST"):
            handoff.import_packet(inbox, self.write_json("bad.json", bad))
        view = handoff.view_inbox(inbox)
        self.assertEqual(view["rows"][0]["bag_id"], "B")
        self.assertEqual((view["packet_no"], view["confirmed_seq"]), (1, 1))

    def test_ack_rejects_wrong_desk_and_unknown_stream(self):
        _, _, receipt = self._sender_with_pending()
        handoff.open_stream(self.archive_path, "STREAM-CCC", "CCC")
        wrong_desk = dict(receipt, desk="CCC")
        with self.assertRaisesRegex(ValueError, "desk CCC"):
            handoff.acknowledge(self.archive_path, "STREAM-BBB",
                                self.write_json("wd.json", wrong_desk))
        with self.assertRaisesRegex(ValueError, "unknown handoff stream"):
            handoff.acknowledge(self.archive_path, "NOPE",
                                self.write_json("nd.json", receipt))

    def test_only_matching_pending_receipt_advances_stream(self):
        inbox, packet_path, receipt = self._sender_with_pending()
        tampered = dict(receipt, to_seq=99)
        with self.assertRaisesRegex(ValueError, "does not match pending"):
            handoff.acknowledge(self.archive_path, "STREAM-BBB",
                                self.write_json("t.json", tampered))
        # The stream still has packet 1 pending: cutoff 2 is refused.
        self.apply(flight_correction())
        with self.assertRaisesRegex(ValueError, "awaiting acknowledgement"):
            handoff.export_packet(self.archive_path, "STREAM-BBB", 2,
                                  self.root / "jump.json")

    def test_other_streams_and_archive_advance_while_one_is_pending(self):
        self.init_archive()
        handoff.open_stream(self.archive_path, "STREAM-BBB", "BBB")
        handoff.open_stream(self.archive_path, "STREAM-CCC", "CCC")
        ccc_inbox = self.root / "inbox-ccc"
        handoff.create_inbox(ccc_inbox, "STREAM-CCC", "CCC")
        self.export("STREAM-BBB", 1)  # pending, never acknowledged
        # The source archive keeps advancing.
        self.assertEqual(self.apply(flight_correction())[0], 2)
        # A different stream is unaffected and advances independently.
        first = self.export("STREAM-CCC", 1)
        self.assertEqual(self.read(first)["packet_no"], 1)
        handoff.acknowledge(
            self.archive_path, "STREAM-CCC",
            self.write_json("ccc-r1.json", handoff.import_packet(ccc_inbox, first)))
        second = self.read(self.export("STREAM-CCC", 2))
        self.assertEqual((second["packet_no"], second["to_seq"]), (2, 2))
        self.assertEqual(second["changes"][0]["latest_scan"]["scan_id"], "S-CCC")

    def test_missing_packet_field_rejected(self):
        self.init_archive()
        handoff.open_stream(self.archive_path, "STREAM-BBB", "BBB")
        inbox = self.root / "inbox"
        handoff.create_inbox(inbox, "STREAM-BBB", "BBB")
        packet = self.read(self.export("STREAM-BBB", 1))
        for field in ("from_seq", "to_seq", "prev_packet_no", "changes"):
            incomplete = {key: value for key, value in packet.items() if key != field}
            with self.assertRaisesRegex(ValueError, "missing or unknown"):
                handoff.import_packet(inbox, self.write_json(f"m-{field}.json", incomplete))
        self.assertEqual(handoff.view_inbox(inbox)["packet_no"], 0)

    # ---- CLI ----------------------------------------------------------------

    def test_cli_end_to_end(self):
        self.init_archive()
        out = self.run_cli("handoff-open", "--archive", str(self.archive_path),
                           "--stream", "STREAM-BBB", "--desk", "BBB")
        self.assertEqual(json.loads(out)["desk"], "BBB")
        inbox = self.root / "inbox"
        out = self.run_cli("handoff-inbox", "--inbox", str(inbox),
                           "--stream", "STREAM-BBB", "--desk", "BBB")
        self.assertEqual(json.loads(out)["packet_no"], 0)
        out = self.run_cli("handoff-view", "--inbox", str(inbox))
        self.assertEqual(json.loads(out), {"stream_id": "STREAM-BBB", "desk": "BBB",
                                           "packet_no": 0, "confirmed_seq": 0,
                                           "rows": []})
        packet = self.root / "cli-packet.json"
        out = self.run_cli("handoff-export", "--archive", str(self.archive_path),
                           "--stream", "STREAM-BBB", "--as-of", "1",
                           "--output", str(packet))
        self.assertEqual(json.loads(out)["packet_no"], 1)
        out = self.run_cli("handoff-import", "--inbox", str(inbox),
                           "--packet", str(packet))
        receipt = json.loads(out)
        self.assertEqual(receipt["to_seq"], 1)
        receipt_path = self.write_json("cli-receipt.json", receipt)
        out = self.run_cli("handoff-ack", "--archive", str(self.archive_path),
                           "--stream", "STREAM-BBB", "--receipt", str(receipt_path))
        self.assertEqual(json.loads(out)["confirmed_seq"], 1)
        view = json.loads(self.run_cli("handoff-view", "--inbox", str(inbox)))
        self.assertEqual(view["rows"][0]["latest_scan"]["scan_id"], "S-BBB")


class HandoffAcceptanceCase(HandoffFixtures):
    """The required cross-station handoff, driven through real processes."""

    def run_process(self, *argv):
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        result = subprocess.run(
            [sys.executable, "-m", "baggage.cli", *argv],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
        return result

    def ok(self, *argv):
        result = self.run_process(*argv)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def fail(self, *argv):
        result = self.run_process(*argv)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(result.stdout.startswith("bag-reconcile: "), result.stdout)
        return result.stdout.strip()

    def test_cross_station_handoff_scenario(self):
        self.init_archive()
        bbb_inbox = self.root / "inbox-bbb"
        ccc_inbox = self.root / "inbox-ccc"
        self.ok("handoff-open", "--archive", str(self.archive_path),
                "--stream", "STREAM-BBB", "--desk", "BBB")
        self.ok("handoff-open", "--archive", str(self.archive_path),
                "--stream", "STREAM-CCC", "--desk", "CCC")
        self.ok("handoff-inbox", "--inbox", str(bbb_inbox),
                "--stream", "STREAM-BBB", "--desk", "BBB")
        self.ok("handoff-inbox", "--inbox", str(ccc_inbox),
                "--stream", "STREAM-CCC", "--desk", "CCC")

        # BBB's first packet: BBB evidence even though a later CCC scan exists.
        bbb_p1 = self.root / "bbb-1.json"
        self.ok("handoff-export", "--archive", str(self.archive_path),
                "--stream", "STREAM-BBB", "--as-of", "1", "--output", str(bbb_p1))
        packet = self.read(bbb_p1)
        self.assertEqual(packet["changes"][0]["latest_scan"]["scan_id"], "S-BBB")

        # CCC's first packet at the same cutoff holds no bag (F2 leaves BBB).
        ccc_p1 = self.root / "ccc-1.json"
        self.ok("handoff-export", "--archive", str(self.archive_path),
                "--stream", "STREAM-CCC", "--as-of", "1", "--output", str(ccc_p1))
        self.assertEqual(self.read(ccc_p1)["changes"], [])

        # BBB receives packet 1 but its receipt is lost (never acknowledged).
        bbb_receipt = self.ok("handoff-import", "--inbox", str(bbb_inbox),
                              "--packet", str(bbb_p1))
        self.assertEqual(bbb_receipt["to_seq"], 1)
        self.assertEqual(self.ok("handoff-view", "--inbox", str(bbb_inbox))
                         ["rows"][0]["bag_id"], "B")

        # CCC confirms its empty first packet, then can receive the bag.
        ccc_receipt = self.ok("handoff-import", "--inbox", str(ccc_inbox),
                              "--packet", str(ccc_p1))
        self.ok("handoff-ack", "--archive", str(self.archive_path),
                "--stream", "STREAM-CCC",
                "--receipt", str(self.write_json("ccc-r1.json", ccc_receipt)))

        # The flight is corrected to depart from CCC (archive sequence 2).
        self.apply(flight_correction())

        ccc_p2 = self.root / "ccc-2.json"
        self.ok("handoff-export", "--archive", str(self.archive_path),
                "--stream", "STREAM-CCC", "--as-of", "2", "--output", str(ccc_p2))
        ccc_packet = self.read(ccc_p2)
        self.assertEqual(ccc_packet["changes"][0]["latest_scan"]["scan_id"], "S-CCC")
        ccc_receipt2 = self.ok("handoff-import", "--inbox", str(ccc_inbox),
                               "--packet", str(ccc_p2))
        self.ok("handoff-ack", "--archive", str(self.archive_path),
                "--stream", "STREAM-CCC",
                "--receipt", str(self.write_json("ccc-r2.json", ccc_receipt2)))
        ccc_view = self.ok("handoff-view", "--inbox", str(ccc_inbox))
        self.assertEqual(ccc_view["rows"][0]["latest_scan"]["scan_id"], "S-CCC")

        # BBB cannot jump to the corrected cutoff while packet 1 is pending.
        message = self.fail("handoff-export", "--archive", str(self.archive_path),
                            "--stream", "STREAM-BBB", "--as-of", "2",
                            "--output", str(self.root / "bbb-jump.json"))
        self.assertIn("awaiting acknowledgement", message)
        self.assertFalse((self.root / "bbb-jump.json").exists())

        # Re-sending the original packet re-issues its receipt byte-for-byte.
        resent = self.root / "bbb-1-again.json"
        self.ok("handoff-export", "--archive", str(self.archive_path),
                "--stream", "STREAM-BBB", "--as-of", "1", "--output", str(resent))
        self.assertEqual(resent.read_bytes(), bbb_p1.read_bytes())
        reissued = self.ok("handoff-import", "--inbox", str(bbb_inbox),
                           "--packet", str(resent))
        self.assertEqual(reissued, bbb_receipt)
        self.ok("handoff-ack", "--archive", str(self.archive_path),
                "--stream", "STREAM-BBB",
                "--receipt", str(self.write_json("bbb-r1.json", reissued)))

        # The next BBB packet only retracts B, with no new destination.
        bbb_p2 = self.root / "bbb-2.json"
        self.ok("handoff-export", "--archive", str(self.archive_path),
                "--stream", "STREAM-BBB", "--as-of", "2", "--output", str(bbb_p2))
        second = self.read(bbb_p2)
        self.assertEqual(second["changes"], [])
        self.assertEqual(second["retractions"], ["B"])
        self.assertNotIn("CCC", json.dumps(second["retractions"]))
        receipt2 = self.ok("handoff-import", "--inbox", str(bbb_inbox),
                           "--packet", str(bbb_p2))
        self.ok("handoff-ack", "--archive", str(self.archive_path),
                "--stream", "STREAM-BBB",
                "--receipt", str(self.write_json("bbb-r2.json", receipt2)))
        bbb_view = self.ok("handoff-view", "--inbox", str(bbb_inbox))
        self.assertEqual((bbb_view["packet_no"], bbb_view["confirmed_seq"],
                          bbb_view["rows"]), (2, 2, []))

        # Re-sending packet 1 after the retraction still returns the first
        # receipt and never resurrects B.
        replay = self.ok("handoff-import", "--inbox", str(bbb_inbox),
                         "--packet", str(bbb_p1))
        self.assertEqual(replay, bbb_receipt)
        self.assertEqual(self.ok("handoff-view", "--inbox", str(bbb_inbox))["rows"], [])

    def test_restart_after_export_failure_and_after_import_failure(self):
        self.init_archive()
        handoff.open_stream(self.archive_path, "STREAM-BBB", "BBB")
        inbox = self.root / "inbox"
        handoff.create_inbox(inbox, "STREAM-BBB", "BBB")

        # Export to an already-present output fails in its own process.
        occupied = self.root / "occupied.json"
        occupied.write_text("keep", encoding="utf-8")
        self.fail("handoff-export", "--archive", str(self.archive_path),
                  "--stream", "STREAM-BBB", "--as-of", "1",
                  "--output", str(occupied))
        self.assertEqual(occupied.read_text(encoding="utf-8"), "keep")
        # A fresh process can still export packet 1 to a clean path.
        packet = self.root / "packet.json"
        self.ok("handoff-export", "--archive", str(self.archive_path),
                "--stream", "STREAM-BBB", "--as-of", "1", "--output", str(packet))

        # A corrupt packet is refused without touching the inbox.
        corrupt = self.read(packet)
        corrupt["changes"][0]["destination"] = "TAMPERED"
        corrupt_path = self.write_json("corrupt.json", corrupt)
        self.fail("handoff-import", "--inbox", str(inbox), "--packet", str(corrupt_path))
        self.assertEqual(self.ok("handoff-view", "--inbox", str(inbox))["packet_no"], 0)

        # After relaunch the same good packet imports and acknowledges.
        receipt = self.ok("handoff-import", "--inbox", str(inbox),
                          "--packet", str(packet))
        self.ok("handoff-ack", "--archive", str(self.archive_path),
                "--stream", "STREAM-BBB",
                "--receipt", str(self.write_json("receipt.json", receipt)))
        self.assertEqual(self.ok("handoff-view", "--inbox", str(inbox))["confirmed_seq"], 1)

    def test_net_change_skips_intermediate_sequences(self):
        # A scan replaced twice and a flight corrected then reverted between
        # cutoff 0 and 3 must export only the net result.
        self.init_archive()
        handoff.open_stream(self.archive_path, "STREAM-BBB", "BBB")
        self.apply({"batch_id": "R1", "scans": [
            {"op": "replace", "scan_id": "S-BBB",
             "scan": {"id": "S-BBB", "bag_id": "B", "station": "BBB",
                      "event_time": "2026-01-01T11:30:00+00:00"}}]})
        self.apply(flight_correction(origin="CCC"))
        self.apply({"batch_id": "R2", "scans": [
            {"op": "replace", "scan_id": "S-BBB",
             "scan": {"id": "S-BBB", "bag_id": "B", "station": "BBB",
                      "event_time": "2026-01-01T11:40:00+00:00"}}],
            "flights": [
                {"flight_id": "F2", "origin": "BBB", "destination": "CCC",
                 "departure": "2026-01-01T12:00:00+00:00",
                 "arrival": "2026-01-01T13:00:00+00:00"}]})
        packet = self.read(self.export("STREAM-BBB", 4))
        row = packet["changes"][0]
        self.assertEqual(row["latest_scan"]["event_time"], "2026-01-01T11:40:00+00:00")
        self.assertEqual(row["latest_scan"]["knowledge_seq"], 4)
        self.assertEqual(row["flight_knowledge_seq"], 4)
        self.assertEqual(packet["retractions"], [])
        self.assertEqual((packet["from_seq"], packet["to_seq"]), (0, 4))


if __name__ == "__main__":
    unittest.main()

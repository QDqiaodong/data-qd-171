"""Offline per-desk evidence handoff for a baggage verification archive.

A *delivery stream* is identified by a sender-chosen ``stream_id`` and a fixed
station code ``desk``.  The sender lives beside an existing archive and exports
self-contained JSON *packets*; the receiver is a standalone ``INBOX`` directory
that never reads the source archive.  Packets carry only the evidence the desk
is responsible for viewing: bags whose *next* flight leaves from that desk and
the latest scan of each such bag *at that desk*.  They never carry other
stations' scans, earlier flight legs, screening records, or loading manifests.

State is kept in two small JSON documents:

* ``<archive>/handoff-sender.json`` -- stream registry, confirmed cutoff and
  the single packet (if any) awaiting acknowledgement.
* ``<inbox>/handoff-inbox.json`` -- the applied copy, its cutoff, and the
  identity/receipt of every packet ever applied.

Both are replaced atomically.  Export publishes the packet with an atomic hard
link that refuses to overwrite, and records the pending packet durably before
the file becomes visible, so a restart still knows what is awaiting a receipt.

Python API (standard library only)::

    open_stream(archive_path, stream_id, desk)
    export_packet(archive_path, stream_id, as_of, output_path)
    create_inbox(inbox_path, stream_id, desk)
    import_packet(inbox_path, packet_path)
    acknowledge(archive_path, stream_id, receipt_path)
    view_inbox(inbox_path)
"""

import fcntl
import hashlib
import json
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from . import archive
from .core import _time

PACKET_FORMAT = "bag-reconcile-handoff/v1"
RECEIPT_FORMAT = "bag-reconcile-handoff-receipt/v1"
SENDER_FILE = "handoff-sender.json"
INBOX_FILE = "handoff-inbox.json"
SENDER_VERSION = 1
INBOX_VERSION = 1
INBOX_LOCK = ".handoff.lock"
HASH_ALGORITHM = "sha256"

_PACKET_KEYS = {
    "format",
    "stream_id",
    "desk",
    "packet_no",
    "prev_packet_no",
    "from_seq",
    "to_seq",
    "changes",
    "retractions",
    "integrity",
}
_ROW_KEYS = {
    "bag_id",
    "next_flight_id",
    "origin",
    "destination",
    "departure",
    "arrival",
    "flight_knowledge_seq",
    "latest_scan",
}
_SCAN_KEYS = {"scan_id", "event_time", "knowledge_seq"}
_RECEIPT_KEYS = {
    "format",
    "stream_id",
    "desk",
    "packet_no",
    "from_seq",
    "to_seq",
    "packet_sha256",
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _require_str(record, key, label):
    value = record.get(key) if isinstance(record, dict) else None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} requires a non-empty string field {key!r}")
    return value


def _require_int(record, key, label, minimum=None):
    value = record.get(key) if isinstance(record, dict) else None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} requires an integer field {key!r}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} field {key!r} must be >= {minimum}")
    return value


def _require_hex(value, label):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{label} requires a lowercase sha256 hex digest")
    return value


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _digest(body):
    return hashlib.sha256(_canonical(body)).hexdigest()


def _packet_bytes(packet):
    return (
        json.dumps(packet, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_bytes(path, payload):
    """Replace ``path`` atomically so readers see the old or the new file."""
    target = Path(path)
    temp = target.parent / f".{target.name}.tmp-{os.getpid()}-{time.time_ns()}"
    try:
        with temp.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
        _fsync_dir(target.parent)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def _atomic_write_json(path, payload):
    _atomic_write_bytes(path, _canonical(payload) + b"\n")


def _publish_exclusive(target_path, payload):
    """Publish a complete file without ever overwriting an existing one.

    The bytes are written and fsynced to a sibling temporary file first, then
    atomically linked into place.  A crashed or failed export therefore never
    leaves a partial packet at the destination path.
    """
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"output already exists: {target}")
    temp = target.parent / f".{target.name}.tmp-{os.getpid()}-{time.time_ns()}"
    try:
        with temp.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, target)
        _fsync_dir(target.parent)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# sender state
# ---------------------------------------------------------------------------


def _sender_path(archive_path):
    return Path(archive_path) / SENDER_FILE


def _empty_sender():
    return {"version": SENDER_VERSION, "streams": {}}


def _load_sender(archive_path, allow_missing=False):
    path = _sender_path(archive_path)
    if not path.is_file():
        if allow_missing:
            return _empty_sender()
        raise ValueError(f"no handoff streams are registered for archive {archive_path}")
    try:
        state = archive._load_json(path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"corrupt handoff sender state: {exc}") from exc
    if not isinstance(state, dict) or state.get("version") != SENDER_VERSION:
        raise ValueError("corrupt handoff sender state: unsupported layout")
    if not isinstance(state.get("streams"), dict):
        raise ValueError("corrupt handoff sender state: malformed streams")
    return state


def _save_sender(archive_path, state):
    _atomic_write_json(_sender_path(archive_path), state)


def _stream(state, stream_id):
    stream = state["streams"].get(stream_id)
    if stream is None:
        raise ValueError(f"unknown handoff stream {stream_id}")
    return stream


# ---------------------------------------------------------------------------
# station evidence
# ---------------------------------------------------------------------------


def _station_rows(bags, scans, flights, desk):
    """Rows this desk is responsible for viewing, keyed by ``bag_id``.

    Scope is decided by the effective flight plan: a bag belongs only when its
    non-empty ``next_flight_id`` has ``origin == desk``.  The delivered scan is
    the latest active scan *at this desk* ordered by the real offset-aware
    instant and ``scan_id``; a later scan at another station is irrelevant.
    """
    rows = {}
    for bag in bags:
        next_flight_id = bag.get("next_flight_id")
        if not next_flight_id:
            continue
        flight = flights.get(next_flight_id)
        if flight is None:
            continue
        record = flight["record"]
        if record["origin"] != desk:
            continue
        latest = None
        for scan_id, entry in scans.items():
            scan = entry["record"]
            if scan["bag_id"] != bag["id"] or scan["station"] != desk:
                continue
            key = (_time(scan["event_time"]), scan_id)
            if latest is None or key > latest[0]:
                latest = (key, scan_id, entry)
        rows[bag["id"]] = {
            "bag_id": bag["id"],
            "next_flight_id": next_flight_id,
            "origin": record["origin"],
            "destination": record["destination"],
            "departure": record["departure"],
            "arrival": record["arrival"],
            "flight_knowledge_seq": flight["knowledge_seq"],
            "latest_scan": None
            if latest is None
            else {
                "scan_id": latest[1],
                "event_time": latest[2]["record"]["event_time"],
                "knowledge_seq": latest[2]["knowledge_seq"],
            },
        }
    return rows


def _build_packet(stream, packet_no, from_seq, to_seq, changes, retractions):
    body = {
        "format": PACKET_FORMAT,
        "stream_id": stream["stream_id"],
        "desk": stream["desk"],
        "packet_no": packet_no,
        "prev_packet_no": stream["last_packet_no"],
        "from_seq": from_seq,
        "to_seq": to_seq,
        "changes": changes,
        "retractions": retractions,
    }
    packet = dict(body)
    packet["integrity"] = {"algorithm": HASH_ALGORITHM, "value": _digest(body)}
    return packet, packet["integrity"]["value"]


# ---------------------------------------------------------------------------
# 1. sender registration
# ---------------------------------------------------------------------------


def open_stream(archive_path, stream_id, desk):
    """Register one sender stream beside an existing archive."""
    stream_id = _require_str({"stream_id": stream_id}, "stream_id", "handoff stream")
    desk = _require_str({"desk": desk}, "desk", "handoff stream")
    with archive._locked(archive_path):
        archive._load_events(archive_path)
        state = _load_sender(archive_path, allow_missing=True)
        if stream_id in state["streams"]:
            raise ValueError(
                f"handoff stream {stream_id} is already registered; "
                "its station cannot change and the id cannot be reused"
            )
        state["streams"][stream_id] = {
            "stream_id": stream_id,
            "desk": desk,
            "confirmed_seq": 0,
            "next_packet_no": 1,
            "last_packet_no": 0,
            "pending": None,
            "confirmed_packets": {},
        }
        _save_sender(archive_path, state)
    return {
        "stream_id": stream_id,
        "desk": desk,
        "confirmed_seq": 0,
        "packet_no": 0,
    }


# ---------------------------------------------------------------------------
# 2. export
# ---------------------------------------------------------------------------


def export_packet(archive_path, stream_id, as_of, output_path):
    """Export the next packet for a stream, or re-export the pending one."""
    with archive._locked(archive_path):
        events = archive._load_events(archive_path)
        max_seq = events[-1]["knowledge_seq"]
        if (
            not isinstance(as_of, int)
            or isinstance(as_of, bool)
            or as_of < 1
            or as_of > max_seq
        ):
            raise ValueError(f"unknown or unaccepted knowledge_seq: {as_of}")
        state = _load_sender(archive_path)
        stream = _stream(state, stream_id)
        target = Path(output_path)
        if target.exists():
            raise FileExistsError(f"output already exists: {target}")

        pending = stream["pending"]
        if pending is not None:
            if as_of != pending["to_seq"]:
                raise ValueError(
                    f"stream {stream_id} packet {pending['packet_no']} is awaiting "
                    f"acknowledgement; only cutoff {pending['to_seq']} may be re-exported"
                )
            packet = pending["packet"]
            digest = pending["packet_sha256"]
            _check_pending_packet(packet, digest)
            _publish_exclusive(target, _packet_bytes(packet))
            return _export_result(stream, packet, digest, target)

        if as_of <= stream["confirmed_seq"]:
            raise ValueError(
                f"cutoff {as_of} must be strictly greater than the confirmed "
                f"cutoff {stream['confirmed_seq']} for stream {stream_id}"
            )

        if stream["confirmed_seq"] == 0:
            # Nothing has been delivered yet: the confirmed copy is empty.
            base_rows = {}
        else:
            bags, scans, flights = archive._project(events, stream["confirmed_seq"])
            base_rows = _station_rows(bags, scans, flights, stream["desk"])
        bags, scans, flights = archive._project(events, as_of)
        target_rows = _station_rows(bags, scans, flights, stream["desk"])

        changes = [
            target_rows[bag_id]
            for bag_id in sorted(target_rows)
            if base_rows.get(bag_id) != target_rows[bag_id]
        ]
        retractions = sorted(set(base_rows) - set(target_rows))

        packet_no = stream["next_packet_no"]
        packet, digest = _build_packet(
            stream,
            packet_no,
            from_seq=stream["confirmed_seq"],
            to_seq=as_of,
            changes=changes,
            retractions=retractions,
        )

        previous = json.loads(json.dumps(stream))
        stream = {
            **stream,
            "pending": {
                "packet_no": packet_no,
                "from_seq": stream["confirmed_seq"],
                "to_seq": as_of,
                "packet_sha256": digest,
                "packet": packet,
            },
        }
        state["streams"][stream_id] = stream
        # Durable before the file is visible: a restart still knows it is pending.
        _save_sender(archive_path, state)
        try:
            _publish_exclusive(target, _packet_bytes(packet))
        except BaseException:
            published = target.is_file() and target.read_bytes() == _packet_bytes(packet)
            if not published:
                # Nothing visible was published, so do not leave a phantom
                # pending packet that would block other cutoffs.
                state["streams"][stream_id] = previous
                try:
                    _save_sender(archive_path, state)
                except OSError:
                    pass
            raise
        return _export_result(stream, packet, digest, target)


def _check_pending_packet(packet, digest):
    body = {key: value for key, value in packet.items() if key != "integrity"}
    if _digest(body) != digest:
        raise ValueError("corrupt handoff sender state: pending packet no longer matches")


def _export_result(stream, packet, digest, target):
    return {
        "stream_id": stream["stream_id"],
        "desk": stream["desk"],
        "packet_no": packet["packet_no"],
        "prev_packet_no": packet["prev_packet_no"],
        "from_seq": packet["from_seq"],
        "to_seq": packet["to_seq"],
        "changes": len(packet["changes"]),
        "retractions": len(packet["retractions"]),
        "packet_sha256": digest,
        "output": str(target),
    }


# ---------------------------------------------------------------------------
# 3. acknowledgement
# ---------------------------------------------------------------------------


def _validate_receipt(receipt):
    if not isinstance(receipt, dict):
        raise ValueError("handoff receipt must be a JSON object")
    if set(receipt) != _RECEIPT_KEYS:
        raise ValueError("handoff receipt has missing or unknown fields")
    if receipt.get("format") != RECEIPT_FORMAT:
        raise ValueError("unsupported handoff receipt format")
    _require_str(receipt, "stream_id", "handoff receipt")
    _require_str(receipt, "desk", "handoff receipt")
    _require_int(receipt, "packet_no", "handoff receipt", minimum=1)
    _require_int(receipt, "from_seq", "handoff receipt", minimum=0)
    _require_int(receipt, "to_seq", "handoff receipt", minimum=1)
    _require_hex(receipt.get("packet_sha256"), "handoff receipt")
    return receipt


def acknowledge(archive_path, stream_id, receipt_path):
    """Advance a stream only on a receipt that exactly matches a packet we sent."""
    receipt = archive._load_json(receipt_path)
    _validate_receipt(receipt)
    with archive._locked(archive_path):
        state = _load_sender(archive_path)
        stream = _stream(state, stream_id)
        if receipt["stream_id"] != stream_id:
            raise ValueError(
                f"receipt is for stream {receipt['stream_id']}, not {stream_id}"
            )
        if receipt["desk"] != stream["desk"]:
            raise ValueError(
                f"receipt is for desk {receipt['desk']}, not {stream['desk']}"
            )

        pending = stream["pending"]
        if pending is not None and receipt["packet_no"] == pending["packet_no"]:
            if (
                receipt["from_seq"] != pending["from_seq"]
                or receipt["to_seq"] != pending["to_seq"]
                or receipt["packet_sha256"] != pending["packet_sha256"]
            ):
                raise ValueError(
                    f"receipt does not match pending packet {pending['packet_no']}"
                )
            stream = {
                **stream,
                "confirmed_seq": pending["to_seq"],
                "last_packet_no": pending["packet_no"],
                "next_packet_no": pending["packet_no"] + 1,
                "pending": None,
                "confirmed_packets": {
                    **stream["confirmed_packets"],
                    str(pending["packet_no"]): {
                        "from_seq": pending["from_seq"],
                        "to_seq": pending["to_seq"],
                        "packet_sha256": pending["packet_sha256"],
                    },
                },
            }
            state["streams"][stream_id] = stream
            _save_sender(archive_path, state)
            return {
                "stream_id": stream_id,
                "desk": stream["desk"],
                "packet_no": pending["packet_no"],
                "confirmed_seq": stream["confirmed_seq"],
                "pending": False,
                "advanced": True,
            }

        confirmed = stream["confirmed_packets"].get(str(receipt["packet_no"]))
        if confirmed is not None and (
            confirmed["from_seq"] == receipt["from_seq"]
            and confirmed["to_seq"] == receipt["to_seq"]
            and confirmed["packet_sha256"] == receipt["packet_sha256"]
        ):
            # A genuine receipt for an already-confirmed packet.  It is
            # accepted but never advances or rolls back the stream, even when
            # a later packet is already awaiting acknowledgement.
            return {
                "stream_id": stream_id,
                "desk": stream["desk"],
                "packet_no": receipt["packet_no"],
                "confirmed_seq": stream["confirmed_seq"],
                "pending": pending is not None,
                "advanced": False,
            }

        if pending is not None:
            raise ValueError(
                f"receipt does not match pending packet {pending['packet_no']} "
                f"for stream {stream_id}"
            )
        raise ValueError(
            f"receipt does not match any packet confirmed for stream {stream_id}"
        )


# ---------------------------------------------------------------------------
# receiver (inbox) state
# ---------------------------------------------------------------------------


def _inbox_path(inbox_path):
    return Path(inbox_path) / INBOX_FILE


def _load_inbox(inbox_path):
    path = _inbox_path(inbox_path)
    if not path.is_file():
        raise ValueError(f"not a handoff inbox: {inbox_path}")
    try:
        state = archive._load_json(path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"corrupt handoff inbox: {exc}") from exc
    if not isinstance(state, dict) or state.get("version") != INBOX_VERSION:
        raise ValueError("corrupt handoff inbox: unsupported layout")
    for key in ("stream_id", "desk"):
        if not isinstance(state.get(key), str) or not state[key]:
            raise ValueError("corrupt handoff inbox: malformed identity")
    for key in ("current_packet_no", "confirmed_seq"):
        if not isinstance(state.get(key), int) or isinstance(state[key], bool):
            raise ValueError(f"corrupt handoff inbox: malformed {key}")
    if not isinstance(state.get("rows"), dict) or not isinstance(state.get("packets"), dict):
        raise ValueError("corrupt handoff inbox: malformed state")
    return state


def _save_inbox(inbox_path, state):
    _atomic_write_json(_inbox_path(inbox_path), state)


@contextmanager
def _inbox_locked(inbox_path):
    path = Path(inbox_path)
    if not _inbox_path(path).is_file():
        raise ValueError(f"not a handoff inbox: {inbox_path}")
    with (path / INBOX_LOCK).open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def create_inbox(inbox_path, stream_id, desk):
    """Pre-register an empty receiver for exactly one stream."""
    stream_id = _require_str({"stream_id": stream_id}, "stream_id", "handoff inbox")
    desk = _require_str({"desk": desk}, "desk", "handoff inbox")
    inbox = Path(inbox_path)
    if inbox.exists():
        raise ValueError(f"inbox already exists: {inbox}")
    inbox.parent.mkdir(parents=True, exist_ok=True)
    staging = inbox.parent / f".{inbox.name}.init-{os.getpid()}-{time.time_ns()}"
    if staging.exists():
        raise ValueError(f"inbox already exists: {inbox}")
    staging.mkdir()
    try:
        state = {
            "version": INBOX_VERSION,
            "stream_id": stream_id,
            "desk": desk,
            "current_packet_no": 0,
            "confirmed_seq": 0,
            "rows": {},
            "packets": {},
        }
        _atomic_write_json(staging / INBOX_FILE, state)
        (staging / INBOX_LOCK).write_text("", encoding="utf-8")
        if inbox.exists():
            raise ValueError(f"inbox already exists: {inbox}")
        os.replace(staging, inbox)
        _fsync_dir(inbox.parent)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "stream_id": stream_id,
        "desk": desk,
        "packet_no": 0,
        "confirmed_seq": 0,
    }


# ---------------------------------------------------------------------------
# packet validation / import
# ---------------------------------------------------------------------------


def _validate_scan_snapshot(value, label):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{label} latest_scan must be null or an object")
    if set(value) != _SCAN_KEYS:
        raise ValueError(f"{label} latest_scan has missing or unknown fields")
    _require_str(value, "scan_id", f"{label} latest_scan")
    _time(_require_str(value, "event_time", f"{label} latest_scan"))
    _require_int(value, "knowledge_seq", f"{label} latest_scan", minimum=1)
    return {
        "scan_id": value["scan_id"],
        "event_time": value["event_time"],
        "knowledge_seq": value["knowledge_seq"],
    }


def _validate_row(value, desk):
    if not isinstance(value, dict):
        raise ValueError("handoff change must be a JSON object")
    if set(value) != _ROW_KEYS:
        raise ValueError("handoff change has missing or unknown fields")
    bag_id = _require_str(value, "bag_id", "handoff change")
    _require_str(value, "next_flight_id", "handoff change")
    origin = _require_str(value, "origin", "handoff change")
    if origin != desk:
        raise ValueError(
            f"handoff change {bag_id} origin {origin} does not match desk {desk}"
        )
    _require_str(value, "destination", "handoff change")
    departure = _time(_require_str(value, "departure", "handoff change"))
    arrival = _time(_require_str(value, "arrival", "handoff change"))
    if arrival < departure:
        raise ValueError(f"handoff change {bag_id} arrival precedes departure")
    _require_int(value, "flight_knowledge_seq", "handoff change", minimum=1)
    scan = _validate_scan_snapshot(value.get("latest_scan"), f"handoff change {bag_id}")
    return {
        "bag_id": bag_id,
        "next_flight_id": value["next_flight_id"],
        "origin": origin,
        "destination": value["destination"],
        "departure": value["departure"],
        "arrival": value["arrival"],
        "flight_knowledge_seq": value["flight_knowledge_seq"],
        "latest_scan": scan,
    }


def _validate_packet_shape(packet):
    if not isinstance(packet, dict):
        raise ValueError("handoff packet must be a JSON object")
    if set(packet) != _PACKET_KEYS:
        raise ValueError("handoff packet has missing or unknown fields")
    if packet.get("format") != PACKET_FORMAT:
        raise ValueError("unsupported handoff packet format")
    _require_str(packet, "stream_id", "handoff packet")
    desk = _require_str(packet, "desk", "handoff packet")
    _require_int(packet, "packet_no", "handoff packet", minimum=1)
    _require_int(packet, "prev_packet_no", "handoff packet", minimum=0)
    _require_int(packet, "from_seq", "handoff packet", minimum=0)
    _require_int(packet, "to_seq", "handoff packet", minimum=1)
    if packet["to_seq"] <= packet["from_seq"]:
        raise ValueError("handoff packet to_seq must be strictly greater than from_seq")

    changes = packet.get("changes")
    retractions = packet.get("retractions")
    if not isinstance(changes, list) or not isinstance(retractions, list):
        raise ValueError("handoff packet changes and retractions must be lists")

    rows = {}
    seen = set()
    for value in changes:
        row = _validate_row(value, desk)
        if row["bag_id"] in seen:
            raise ValueError(
                f"bag {row['bag_id']} appears more than once in the packet"
            )
        seen.add(row["bag_id"])
        rows[row["bag_id"]] = row

    retracted = []
    for value in retractions:
        if not isinstance(value, str) or not value:
            raise ValueError("handoff retractions must be non-empty bag ids")
        if value in seen:
            raise ValueError(f"bag {value} appears more than once in the packet")
        seen.add(value)
        retracted.append(value)

    integrity = packet.get("integrity")
    if not isinstance(integrity, dict) or set(integrity) != {"algorithm", "value"}:
        raise ValueError("handoff packet integrity block is malformed")
    if integrity.get("algorithm") != HASH_ALGORITHM:
        raise ValueError("unsupported handoff integrity algorithm")
    _require_hex(integrity.get("value"), "handoff packet integrity")

    body = {key: value for key, value in packet.items() if key != "integrity"}
    digest = _digest(body)
    if digest != integrity["value"]:
        raise ValueError("handoff packet integrity check failed")
    return rows, retracted, digest


def _receipt_for(packet, digest):
    return {
        "format": RECEIPT_FORMAT,
        "stream_id": packet["stream_id"],
        "desk": packet["desk"],
        "packet_no": packet["packet_no"],
        "from_seq": packet["from_seq"],
        "to_seq": packet["to_seq"],
        "packet_sha256": digest,
    }


def import_packet(inbox_path, packet_path):
    """Apply one packet, returning the receipt that binds its content."""
    packet = archive._load_json(packet_path)
    rows, retracted, digest = _validate_packet_shape(packet)
    with _inbox_locked(inbox_path):
        state = _load_inbox(inbox_path)
        if packet["stream_id"] != state["stream_id"]:
            raise ValueError(
                f"packet is for stream {packet['stream_id']}, "
                f"but this inbox carries {state['stream_id']}"
            )
        if packet["desk"] != state["desk"]:
            raise ValueError(
                f"packet is for desk {packet['desk']}, "
                f"but this inbox is registered for {state['desk']}"
            )

        key = str(packet["packet_no"])
        # The retry rule is deliberately checked before the current position:
        # an already-applied packet (even a much older one) returns its first
        # receipt and never rewinds the copy.  The same number with a different
        # content identity is always rejected.
        applied = state["packets"].get(key)
        if applied is not None:
            if applied["packet_sha256"] == digest:
                return applied["receipt"]
            raise ValueError(
                f"packet {packet['packet_no']} was already applied with "
                "different content"
            )

        current = state["current_packet_no"]
        if packet["packet_no"] != current + 1:
            raise ValueError(
                f"packet {packet['packet_no']} does not immediately follow "
                f"packet {current}"
            )
        if packet["prev_packet_no"] != current:
            raise ValueError(
                f"packet prev_packet_no {packet['prev_packet_no']} does not "
                f"match packet {current}"
            )
        if packet["from_seq"] != state["confirmed_seq"]:
            raise ValueError(
                f"packet base cutoff {packet['from_seq']} does not match the "
                f"inbox cutoff {state['confirmed_seq']}"
            )

        copy = {bag_id: dict(row) for bag_id, row in state["rows"].items()}
        for bag_id in retracted:
            if bag_id not in copy:
                raise ValueError(f"retraction references unknown bag {bag_id}")
            del copy[bag_id]
        for bag_id, row in rows.items():
            copy[bag_id] = row

        receipt = _receipt_for(packet, digest)
        new_state = {
            **state,
            "current_packet_no": packet["packet_no"],
            "confirmed_seq": packet["to_seq"],
            "rows": copy,
            "packets": {
                **state["packets"],
                key: {"packet_sha256": digest, "receipt": receipt},
            },
        }
        # Copy, cutoff and resendable receipt become visible in one atomic step.
        _save_inbox(inbox_path, new_state)
        return receipt


# ---------------------------------------------------------------------------
# 4. view
# ---------------------------------------------------------------------------


def view_inbox(inbox_path):
    """Return the effective packet number, cutoff and this desk's whole copy."""
    with _inbox_locked(inbox_path):
        state = _load_inbox(inbox_path)
        return {
            "stream_id": state["stream_id"],
            "desk": state["desk"],
            "packet_no": state["current_packet_no"],
            "confirmed_seq": state["confirmed_seq"],
            "rows": [state["rows"][bag_id] for bag_id in sorted(state["rows"])],
        }

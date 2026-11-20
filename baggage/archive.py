"""Local append-only verification archive for baggage reconciliation.

The archive is a directory containing a single ``events.jsonl`` event log.
The first event is the validated initial input at ``knowledge_seq`` 1; every
accepted batch appends one event at the next monotonic ``knowledge_seq``.
Failed batches are validated before any write, so the log stays byte-for-byte
unchanged and no sequence number is consumed.
"""

import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

from .core import _time, reconcile

EVENTS_FILE = "events.jsonl"
INITIAL_SEQ = 1
LOCK_FILE = ".bag-reconcile.lock"


@contextmanager
def _locked(archive_path):
    """Hold the archive-wide advisory lock for a consistent read/modify/write.

    Every archive mutation (``archive-apply``) and every loading-workbench
    command takes the same exclusive lock, so a scan/flight read can never be
    interleaved with a half-written occupancy update.  The lock lives beside
    the event log and is an implementation detail, not part of the log.
    """
    path = Path(archive_path)
    if not (path / EVENTS_FILE).is_file():
        raise ValueError(f"not a bag-reconcile archive: {archive_path}")
    with (path / LOCK_FILE).open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_pairs)


def _require_str(record, key, label):
    if not isinstance(record, dict):
        raise ValueError(f"{label} must be a JSON object")
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} requires a non-empty string field {key!r}")
    return value


def _validate_scan(scan, bag_ids):
    sid = _require_str(scan, "id", "scan")
    bag_id = _require_str(scan, "bag_id", "scan")
    _require_str(scan, "station", "scan")
    _time(_require_str(scan, "event_time", "scan"))
    if bag_id not in bag_ids:
        raise ValueError(f"scan references unknown bag {bag_id}")
    return sid


def _validate_flight(flight):
    fid = _require_str(flight, "id", "flight")
    _require_str(flight, "origin", "flight")
    _require_str(flight, "destination", "flight")
    departure = _time(_require_str(flight, "departure", "flight"))
    arrival = _time(_require_str(flight, "arrival", "flight"))
    if arrival < departure:
        raise ValueError(f"flight {fid} arrival must not precede departure")
    return fid


def _validate_initial(data):
    if not isinstance(data, dict):
        raise ValueError("input must be a JSON object")
    flights = data.get("flights")
    bags = data.get("bags")
    scans = data.get("scans")
    if not isinstance(flights, list) or not isinstance(bags, list) or not isinstance(scans, list):
        raise ValueError("flights, bags and scans must be lists")
    flight_ids = set()
    for flight in flights:
        fid = _validate_flight(flight)
        if fid in flight_ids:
            raise ValueError("flight IDs must be unique")
        flight_ids.add(fid)
    bag_ids = set()
    for bag in bags:
        bid = _require_str(bag, "id", "bag")
        if bid in bag_ids:
            raise ValueError("bag IDs must be unique")
        bag_ids.add(bid)
        if _require_str(bag, "flight_id", "bag") not in flight_ids:
            raise ValueError(f"unknown flight for bag {bid}")
        next_flight_id = bag.get("next_flight_id")
        if next_flight_id is not None and (
            not isinstance(next_flight_id, str) or next_flight_id not in flight_ids
        ):
            raise ValueError(f"unknown next flight for bag {bid}")
    scan_ids = set()
    for scan in scans:
        sid = _validate_scan(scan, bag_ids)
        if sid in scan_ids:
            raise ValueError("scan IDs must be unique")
        scan_ids.add(sid)
    return flight_ids, bag_ids, scan_ids


def init_archive(input_path, archive_path):
    """Create a new archive from an existing single-shot input file."""
    data = _load_json(input_path)
    _validate_initial(data)
    # Apply the exact single-shot rule set so an initial input that cannot
    # reconcile (e.g. a missing required bag field) is rejected at init time.
    reconcile(data)
    archive = Path(archive_path)
    if archive.exists():
        raise ValueError(f"archive already exists: {archive}")
    staging = archive.parent / f".{archive.name}.init-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        event = {
            "event": "initial",
            "knowledge_seq": INITIAL_SEQ,
            "flights": data["flights"],
            "bags": data["bags"],
            "scans": data["scans"],
        }
        (staging / EVENTS_FILE).write_text(
            json.dumps(event, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        # The lock file is created inside the staging directory so it moves
        # into place atomically with the events log.
        (staging / LOCK_FILE).write_text("", encoding="utf-8")
        os.replace(staging, archive)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return INITIAL_SEQ


def _load_events(archive_path):
    events_file = Path(archive_path) / EVENTS_FILE
    if not events_file.is_file():
        raise ValueError(f"not a bag-reconcile archive: {archive_path}")
    events = []
    for line_no, line in enumerate(events_file.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line, object_pairs_hook=_pairs)
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt archive event on line {line_no}: {exc}") from exc
        events.append(event)
    if not events or events[0].get("event") != "initial" or events[0].get("knowledge_seq") != 1:
        raise ValueError("archive is missing its initial event at knowledge_seq 1")
    expected_seq = 1
    seen_batch_ids = set()
    for event in events:
        if event.get("knowledge_seq") != expected_seq:
            raise ValueError("archive knowledge_seq is not contiguous")
        if event["event"] == "batch":
            batch_id = event.get("batch_id")
            if batch_id in seen_batch_ids:
                raise ValueError(f"archive contains duplicate batch_id {batch_id}")
            seen_batch_ids.add(batch_id)
        expected_seq += 1
    return events


def _current_state(events):
    initial = events[0]
    bag_ids = {bag["id"] for bag in initial["bags"]}
    active_scans = {scan["id"]: scan for scan in initial["scans"]}
    ever_scan_ids = set(active_scans)
    flights = {flight["id"]: flight for flight in initial["flights"]}
    batch_ids = set()
    for event in events[1:]:
        batch_ids.add(event["batch_id"])
        for change in event["scans"]:
            op = change["op"]
            if op in ("append", "replace"):
                sid = change["scan"]["id"]
                active_scans[sid] = change["scan"]
                ever_scan_ids.add(sid)
            else:
                del active_scans[change["scan_id"]]
        for change in event["flights"]:
            flights[change["flight_id"]] = _flight_record(change)
    return {
        "bag_ids": bag_ids,
        "active_scans": active_scans,
        "ever_scan_ids": ever_scan_ids,
        "flights": flights,
        "batch_ids": batch_ids,
    }


def _flight_record(change):
    return {
        "id": change["flight_id"],
        "origin": change["origin"],
        "destination": change["destination"],
        "departure": change["departure"],
        "arrival": change["arrival"],
    }


def _validate_batch(batch, state):
    if not isinstance(batch, dict):
        raise ValueError("batch must be a JSON object")
    batch_id = batch.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id:
        raise ValueError("batch requires a non-empty string batch_id")
    if batch_id in state["batch_ids"]:
        raise ValueError(f"duplicate batch_id {batch_id}")
    scan_changes = batch.get("scans", [])
    flight_changes = batch.get("flights", [])
    if not isinstance(scan_changes, list) or not isinstance(flight_changes, list):
        raise ValueError("batch scans and flights must be lists")
    if not scan_changes and not flight_changes:
        raise ValueError("batch must contain at least one scan or flight change")

    touched_scans = set()
    for change in scan_changes:
        if not isinstance(change, dict):
            raise ValueError("scan change must be a JSON object")
        op = change.get("op")
        if op == "append":
            sid = _validate_scan(change.get("scan"), state["bag_ids"])
            if sid in state["ever_scan_ids"]:
                raise ValueError(f"scan id {sid} has already been used in the archive")
        elif op == "replace":
            sid = _require_str(change, "scan_id", "scan change")
            scan = change.get("scan")
            _validate_scan(scan, state["bag_ids"])
            if scan["id"] != sid:
                raise ValueError(f"replace scan must keep scan_id {sid}")
            if sid not in state["active_scans"]:
                raise ValueError(f"scan {sid} is not active at batch start")
        elif op == "retract":
            sid = _require_str(change, "scan_id", "scan change")
            if sid not in state["active_scans"]:
                raise ValueError(f"scan {sid} is not active at batch start")
        else:
            raise ValueError(f"unknown scan op {op!r}")
        if sid in touched_scans:
            raise ValueError(f"scan {sid} appears more than once in the same batch")
        touched_scans.add(sid)

    touched_flights = set()
    for change in flight_changes:
        if not isinstance(change, dict):
            raise ValueError("flight change must be a JSON object")
        fid = _require_str(change, "flight_id", "flight change")
        if fid not in state["flights"]:
            raise ValueError(f"flight {fid} is not known in the archive")
        _validate_flight(_flight_record(change))
        if fid in touched_flights:
            raise ValueError(f"flight {fid} appears more than once in the same batch")
        touched_flights.add(fid)
    return batch_id, scan_changes, flight_changes


def apply_batch(archive_path, batch_path):
    """Validate and append one batch; returns the assigned knowledge_seq."""
    with _locked(archive_path):
        events = _load_events(archive_path)
        batch = _load_json(batch_path)
        state = _current_state(events)
        # Everything is validated before the archive file is opened for writing.
        batch_id, scan_changes, flight_changes = _validate_batch(batch, state)
        seq = events[-1]["knowledge_seq"] + 1
        event = {
            "event": "batch",
            "knowledge_seq": seq,
            "batch_id": batch_id,
            "scans": scan_changes,
            "flights": flight_changes,
        }
        events_file = Path(archive_path) / EVENTS_FILE
        with events_file.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return seq, batch_id


def _project(events, as_of):
    initial = events[0]
    bags = initial["bags"]
    scans = {
        scan["id"]: {"record": scan, "knowledge_seq": INITIAL_SEQ, "batch_id": None}
        for scan in initial["scans"]
    }
    flights = {
        flight["id"]: {"record": flight, "knowledge_seq": INITIAL_SEQ, "batch_id": None}
        for flight in initial["flights"]
    }
    for event in events[1:]:
        seq = event["knowledge_seq"]
        if seq > as_of:
            break
        batch_id = event["batch_id"]
        for change in event["scans"]:
            op = change["op"]
            if op in ("append", "replace"):
                sid = change["scan"]["id"]
                scans[sid] = {"record": change["scan"], "knowledge_seq": seq, "batch_id": batch_id}
            else:
                del scans[change["scan_id"]]
        for change in event["flights"]:
            fid = change["flight_id"]
            flights[fid] = {
                "record": _flight_record(change),
                "knowledge_seq": seq,
                "batch_id": batch_id,
            }
    return bags, scans, flights


def build_report(archive_path, as_of):
    with _locked(archive_path):
        return _build_report_locked(archive_path, as_of)


def _build_report_locked(archive_path, as_of):
    events = _load_events(archive_path)
    max_seq = events[-1]["knowledge_seq"]
    if not isinstance(as_of, int) or isinstance(as_of, bool) or as_of < 1 or as_of > max_seq:
        raise ValueError(f"unknown or unaccepted knowledge_seq: {as_of}")
    bags, scans, flights = _project(events, as_of)
    result = reconcile(
        {
            "flights": [entry["record"] for entry in flights.values()],
            "bags": bags,
            "scans": [entry["record"] for entry in scans.values()],
        }
    )
    scan_sources = sorted(
        (
            {
                "scan_id": sid,
                "knowledge_seq": entry["knowledge_seq"],
                "batch_id": entry["batch_id"],
            }
            for sid, entry in scans.items()
        ),
        key=lambda item: item["scan_id"],
    )
    flight_sources = sorted(
        (
            {
                "flight_id": fid,
                "knowledge_seq": entry["knowledge_seq"],
                "batch_id": entry["batch_id"],
            }
            for fid, entry in flights.items()
        ),
        key=lambda item: item["flight_id"],
    )
    return {
        "knowledge_seq": as_of,
        "bags": result["bags"],
        "summary": result["summary"],
        "sources": {"scans": scan_sources, "flights": flight_sources},
    }


def write_report(archive_path, as_of, output_path):
    # Build before touching the output so an invalid seq never creates a file.
    report = build_report(archive_path, as_of)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return report

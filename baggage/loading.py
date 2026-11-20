"""Local loading-release workbench layered on a baggage verification archive.

The workbench stores *manually declared* screening credentials and the release
manifests an operator has explicitly reviewed and committed.  It never infers
physical bag location, never dispatches staff and never contacts an external
service.

State is kept in a single ``loading-workbench.json`` inside the archive
directory and is written atomically.  The archive's ``events.jsonl`` is never
rewritten: scans and flights are read through the existing projection so every
check binds to an explicit, reproducible archive version.  All commands, and
``archive-apply``, take the same archive-wide advisory lock, so a scan/flight
read can never interleave with a half-written workbench update.

Public Python API (standard library only)::

    init_workbench(archive_path)
    apply_screening(archive_path, batch_path)
    review_manifest(archive_path, request_path)
    commit_review(archive_path, review_id)
    cancel_manifest(archive_path, manifest_id)
    manifest_status(archive_path, manifest_id)
"""

import json
import os
import time
from pathlib import Path

from . import archive
from .core import _time

WORKBENCH_FILE = "loading-workbench.json"
WB_VERSION = 1

_DECISIONS = ("pass", "fail")

# Deterministic ordering for the reasons a bag is not eligible.
_REASON_ORDER = (
    "not_assigned_to_flight",
    "no_scan",
    "station_mismatch",
    "scan_after_departure",
    "no_current_clearance",
    "clearance_revoked",
    "clearance_not_pass",
    "clearance_scan_stale",
    "clearance_window_not_covering_departure",
    "occupied",
)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _require_str(record, key, label):
    value = record.get(key) if isinstance(record, dict) else None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} requires a non-empty string field {key!r}")
    return value


def _require_int(record, key, label):
    value = record.get(key) if isinstance(record, dict) else None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} requires an integer field {key!r}")
    return value


def _reasons(items):
    return sorted(set(items), key=_REASON_ORDER.index)


# ---------------------------------------------------------------------------
# paths and durable I/O
# ---------------------------------------------------------------------------


def _workbench_path(archive_path):
    return Path(archive_path) / WORKBENCH_FILE


def _atomic_write(path, payload):
    """Write JSON so readers only ever see the old or the new file."""
    target = Path(path)
    temp = target.parent / f".{target.name}.tmp-{os.getpid()}-{time.time_ns()}"
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def _empty_state():
    return {
        "version": WB_VERSION,
        "screening_batches": [],
        "clearances": {},
        "current_clearance": {},
        "reviews": {},
        "review_counter": 0,
        "manifests": {},
    }


def _load_workbench(archive_path):
    path = _workbench_path(archive_path)
    if not path.is_file():
        raise ValueError(f"loading workbench does not exist for archive {archive_path}")
    try:
        state = archive._load_json(path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"corrupt loading workbench: {exc}") from exc
    if not isinstance(state, dict) or state.get("version") != WB_VERSION:
        raise ValueError("corrupt loading workbench: unsupported layout")
    for key in ("screening_batches", "clearances", "current_clearance", "reviews", "manifests"):
        expected = list if key == "screening_batches" else dict
        if not isinstance(state.get(key), expected):
            raise ValueError("corrupt loading workbench: malformed state")
    if not isinstance(state.get("review_counter", 0), int):
        raise ValueError("corrupt loading workbench: malformed review counter")
    return state


def _save_workbench(archive_path, state):
    _atomic_write(_workbench_path(archive_path), state)


def _load_all(archive_path):
    """Return a consistent archive+workbench view (caller holds the lock)."""
    events = archive._load_events(archive_path)
    state = _load_workbench(archive_path)
    as_of = events[-1]["knowledge_seq"]
    bags, scan_entries, flight_entries = archive._project(events, as_of)
    bags_by_id = {bag["id"]: bag for bag in bags}
    return {
        "events": events,
        "as_of": as_of,
        "bags": bags_by_id,
        "scans": scan_entries,
        "flights": flight_entries,
        "state": state,
    }


# ---------------------------------------------------------------------------
# evidence projection
# ---------------------------------------------------------------------------


def _latest_scans(scan_entries):
    """Map bag_id -> the latest active scan entry, ordered by (event_time, id)."""
    latest = {}
    for sid, entry in scan_entries.items():
        scan = entry["record"]
        key = (_time(scan["event_time"]), sid)
        bag_id = scan["bag_id"]
        if bag_id not in latest or key > latest[bag_id]["key"]:
            latest[bag_id] = {"key": key, "scan_id": sid, "entry": entry}
    return latest


def _scan_snapshot(scan_id, entry):
    scan = entry["record"]
    return {
        "scan_id": scan_id,
        "knowledge_seq": entry["knowledge_seq"],
        "batch_id": entry["batch_id"],
        "station": scan["station"],
        "event_time": scan["event_time"],
    }


def _flight_source(flight_id, entry):
    record = entry["record"]
    return {
        "flight_id": flight_id,
        "knowledge_seq": entry["knowledge_seq"],
        "batch_id": entry["batch_id"],
        "origin": record["origin"],
        "destination": record["destination"],
        "departure": record["departure"],
        "arrival": record["arrival"],
    }


def _clearance_snapshot(record):
    if record is None:
        return None
    status = {"current": "active"}.get(record["status"], record["status"])
    return {
        "id": record["id"],
        "bag_id": record["bag_id"],
        "scan_id": record["scan_id"],
        "scan_knowledge_seq": record["scan_knowledge_seq"],
        "decision": record["decision"],
        "valid_from": record["valid_from"],
        "valid_until": record["valid_until"],
        "status": status,
    }


def _current_pointer(state):
    return state.get("current_clearance", {})


def _current_clearance_record(state, bag_id):
    cid = _current_pointer(state).get(bag_id)
    return state["clearances"].get(cid) if cid else None


# ---------------------------------------------------------------------------
# eligibility
# ---------------------------------------------------------------------------


def _evaluate(bag, flight_id, flight_record, latest, clearance, occupied_by):
    """Return the sorted list of reasons the bag is *not* eligible."""
    departure = _time(flight_record["departure"])
    reasons = []
    if flight_id != bag["flight_id"] and flight_id != bag.get("next_flight_id"):
        reasons.append("not_assigned_to_flight")

    if latest is None:
        reasons.append("no_scan")
    else:
        scan = latest["entry"]["record"]
        if scan["station"] != flight_record["origin"]:
            reasons.append("station_mismatch")
        if _time(scan["event_time"]) > departure:
            reasons.append("scan_after_departure")

    if clearance is None:
        reasons.append("no_current_clearance")
    else:
        revoked = clearance["status"] == "revoked"
        if revoked:
            reasons.append("clearance_revoked")
        elif clearance["decision"] != "pass":
            reasons.append("clearance_not_pass")
        binding_ok = (
            latest is not None
            and clearance["scan_id"] == latest["scan_id"]
            and clearance["scan_knowledge_seq"] == latest["entry"]["knowledge_seq"]
        )
        if not binding_ok:
            reasons.append("clearance_scan_stale")
        window_ok = (
            _time(clearance["valid_from"]) <= departure < _time(clearance["valid_until"])
        )
        if not window_ok:
            reasons.append("clearance_window_not_covering_departure")

    if occupied_by:
        reasons.append("occupied")
    return _reasons(reasons)


def _occupancy(state, exclude_manifest=None):
    """bag_id -> sorted manifest ids currently occupying it (un-cancelled)."""
    occupied = {}
    for manifest_id, manifest in state["manifests"].items():
        if manifest.get("cancelled"):
            continue
        if manifest_id == exclude_manifest:
            continue
        for bag_id in manifest["bag_ids"]:
            occupied.setdefault(bag_id, []).append(manifest_id)
    return {bag_id: sorted(ids) for bag_id, ids in occupied.items()}


# ---------------------------------------------------------------------------
# validation entry points
# ---------------------------------------------------------------------------


def _validate_clearance(clearance, bags, latest, seen_clearance_ids):
    if not isinstance(clearance, dict):
        raise ValueError("clearance must be a JSON object")
    cid = _require_str(clearance, "id", "clearance")
    bag_id = _require_str(clearance, "bag_id", "clearance")
    scan_id = _require_str(clearance, "scan_id", "clearance")
    seq = _require_int(clearance, "scan_knowledge_seq", "clearance")
    decision = clearance.get("decision")
    if decision not in _DECISIONS:
        raise ValueError(f"clearance {cid} decision must be 'pass' or 'fail'")
    valid_from = _time(_require_str(clearance, "valid_from", "clearance"))
    valid_until = _time(_require_str(clearance, "valid_until", "clearance"))
    if not valid_from < valid_until:
        raise ValueError(f"clearance {cid} valid_from must be strictly before valid_until")

    if cid in seen_clearance_ids:
        raise ValueError(f"clearance id {cid} has already been used in this workbench")
    if bag_id not in bags:
        raise ValueError(f"clearance {cid} references unknown bag {bag_id}")
    latest_for_bag = latest.get(bag_id)
    if latest_for_bag is None:
        raise ValueError(f"bag {bag_id} has no scan in the archive")
    if latest_for_bag["scan_id"] != scan_id:
        raise ValueError(
            f"clearance {cid} scan {scan_id} is not the latest scan for bag {bag_id}"
        )
    if latest_for_bag["entry"]["knowledge_seq"] != seq:
        raise ValueError(
            f"clearance {cid} scan_knowledge_seq {seq} is not the latest source "
            f"version {latest_for_bag['entry']['knowledge_seq']} for bag {bag_id}"
        )
    return {
        "id": cid,
        "bag_id": bag_id,
        "scan_id": scan_id,
        "scan_knowledge_seq": seq,
        "decision": decision,
        "valid_from": clearance["valid_from"],
        "valid_until": clearance["valid_until"],
        "status": "current",
    }


def _validate_screening_batch(batch, view):
    state = view["state"]
    if not isinstance(batch, dict):
        raise ValueError("screening batch must be a JSON object")
    batch_id = _require_str(batch, "batch_id", "screening batch")
    if any(entry["batch_id"] == batch_id for entry in state["screening_batches"]):
        raise ValueError(f"duplicate screening batch_id {batch_id}")
    operations = batch.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ValueError("screening batch operations must be a non-empty list")

    bags = view["bags"]
    scans = view["scans"]
    latest = _latest_scans(scans)
    clearances = state["clearances"]

    records = []
    revokes = []
    touched = {}
    seen_clearance_ids = set(clearances)
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("screening operation must be a JSON object")
        op = operation.get("op")
        if op == "record":
            clearance = operation.get("clearance")
            if not isinstance(clearance, dict):
                raise ValueError("record operation requires a clearance object")
            bag_id = _require_str(clearance, "bag_id", "clearance")
            if bag_id in touched:
                raise ValueError(f"bag {bag_id} appears more than once in the same batch")
            touched[bag_id] = True
            record = _validate_clearance(clearance, bags, latest, seen_clearance_ids)
            seen_clearance_ids.add(record["id"])
            records.append(record)
        elif op == "revoke":
            clearance_id = _require_str(operation, "clearance_id", "revoke operation")
            existing = clearances.get(clearance_id)
            if existing is None:
                raise ValueError(f"unknown clearance {clearance_id}")
            if existing["status"] == "superseded":
                raise ValueError(f"clearance {clearance_id} has already been superseded")
            if existing["status"] == "revoked":
                raise ValueError(f"clearance {clearance_id} has already been revoked")
            bag_id = existing["bag_id"]
            if bag_id in touched:
                raise ValueError(f"bag {bag_id} appears more than once in the same batch")
            touched[bag_id] = True
            revokes.append(existing)
        else:
            raise ValueError(f"unknown screening op {op!r}")
    return batch_id, records, revokes


# ---------------------------------------------------------------------------
# 1. initialise
# ---------------------------------------------------------------------------


def init_workbench(archive_path):
    """Create the single loading workbench for an existing archive."""
    with archive._locked(archive_path):
        # Re-validate the archive itself so a corrupt archive is refused.
        archive._load_events(archive_path)
        path = _workbench_path(archive_path)
        if path.exists():
            raise ValueError(f"loading workbench already exists for archive {archive_path}")
        _save_workbench(archive_path, _empty_state())
    return {"archive": str(archive_path), "workbench": str(path)}


# ---------------------------------------------------------------------------
# 2. screening batches
# ---------------------------------------------------------------------------


def apply_screening(archive_path, batch_path):
    """Accept a whole screening batch (records and/or revocations) or none of it."""
    with archive._locked(archive_path):
        view = _load_all(archive_path)
        batch = archive._load_json(batch_path)
        state = view["state"]
        batch_id, records, revokes = _validate_screening_batch(batch, view)

        # Mutate a copy first; nothing becomes visible until the atomic write.
        clearances = {cid: dict(record) for cid, record in state["clearances"].items()}
        pointer = dict(_current_pointer(state))
        for record in records:
            previous = pointer.get(record["bag_id"])
            if previous is not None and clearances[previous]["status"] == "current":
                clearances[previous]["status"] = "superseded"
            clearances[record["id"]] = dict(record)
            pointer[record["bag_id"]] = record["id"]
        for existing in revokes:
            clearances[existing["id"]]["status"] = "revoked"

        state["clearances"] = clearances
        state["current_clearance"] = pointer
        state["screening_batches"].append({"batch_id": batch_id})
        _save_workbench(archive_path, state)
        return {
            "batch_id": batch_id,
            "accepted": True,
            "archive_knowledge_seq": view["as_of"],
            "recorded": [record["id"] for record in records],
            "revoked": [existing["id"] for existing in revokes],
        }


# ---------------------------------------------------------------------------
# 3. review
# ---------------------------------------------------------------------------


def _validate_manifest_request(request, view):
    if not isinstance(request, dict):
        raise ValueError("loading request must be a JSON object")
    manifest_id = _require_str(request, "manifest_id", "loading request")
    flight_id = _require_str(request, "flight_id", "loading request")
    bag_ids = request.get("bag_ids")
    if not isinstance(bag_ids, list) or not bag_ids:
        raise ValueError("loading request bag_ids must be a non-empty list")
    for bag_id in bag_ids:
        if not isinstance(bag_id, str) or not bag_id:
            raise ValueError("loading request bag_ids must be non-empty strings")
    if len(set(bag_ids)) != len(bag_ids):
        raise ValueError("loading request bag_ids must not contain duplicates")
    if flight_id not in view["flights"]:
        raise ValueError(f"loading request references unknown flight {flight_id}")
    for bag_id in bag_ids:
        if bag_id not in view["bags"]:
            raise ValueError(f"loading request references unknown bag {bag_id}")
    state = view["state"]
    if manifest_id in state["manifests"]:
        raise ValueError(f"manifest_id {manifest_id} has already been registered or cancelled")
    return manifest_id, flight_id, sorted(bag_ids)


def _build_review(view, manifest_id, flight_id, bag_ids):
    state = view["state"]
    bags = view["bags"]
    scans = view["scans"]
    flights = view["flights"]
    latest = _latest_scans(scans)
    occupied = _occupancy(state)
    flight_record = flights[flight_id]["record"]

    rows = {}
    for bag_id in bag_ids:
        bag = bags[bag_id]
        current = latest.get(bag_id)
        clearance = _current_clearance_record(state, bag_id)
        occupied_by = occupied.get(bag_id, [])
        reasons = _evaluate(
            bag, flight_id, flight_record, current, clearance, occupied_by
        )
        rows[bag_id] = {
            "bag_id": bag_id,
            "flight_assignment": {
                "flight_id": bag["flight_id"],
                "next_flight_id": bag.get("next_flight_id"),
            },
            "last_scan": None
            if current is None
            else _scan_snapshot(current["scan_id"], current["entry"]),
            "clearance": _clearance_snapshot(clearance),
            "occupied_by": occupied_by,
            "unmet_conditions": reasons,
            "eligible": not reasons,
        }
    committable = all(row["eligible"] for row in rows.values())
    return {
        "manifest_id": manifest_id,
        "flight_id": flight_id,
        "flight_source": _flight_source(flight_id, flights[flight_id]),
        "archive_knowledge_seq": view["as_of"],
        "requested_bag_ids": bag_ids,
        "bags": rows,
        "committable": committable,
    }


def _next_review_id(state):
    # Reviews are never deleted, so a monotonic counter is enough and ids are
    # never reused.
    state["review_counter"] = int(state.get("review_counter", 0)) + 1
    return f"R{state['review_counter']}"


def review_manifest(archive_path, request_path):
    """Save one immutable review of a loading request; it occupies nothing."""
    with archive._locked(archive_path):
        view = _load_all(archive_path)
        request = archive._load_json(request_path)
        manifest_id, flight_id, bag_ids = _validate_manifest_request(request, view)
        review = _build_review(view, manifest_id, flight_id, bag_ids)
        state = view["state"]
        review_id = _next_review_id(state)
        review = {"review_id": review_id, **review}
        state["reviews"][review_id] = review
        _save_workbench(archive_path, state)
        return json.loads(json.dumps(review))


# ---------------------------------------------------------------------------
# 4. commit
# ---------------------------------------------------------------------------


def _evidence_changes(review, view, exclude_manifest):
    """Return the relevant-evidence differences plus a re-evaluation."""
    state = view["state"]
    bags = view["bags"]
    scans = view["scans"]
    flights = view["flights"]
    latest = _latest_scans(scans)
    occupied = _occupancy(state, exclude_manifest=exclude_manifest)

    changes = []
    flight_id = review["flight_id"]
    current_flight = (
        _flight_source(flight_id, flights[flight_id]) if flight_id in flights else None
    )
    if current_flight != review["flight_source"]:
        changes.append(
            {
                "kind": "flight_source",
                "flight_id": flight_id,
                "recorded": review["flight_source"],
                "current": current_flight,
            }
        )

    rows = {}
    for bag_id in review["requested_bag_ids"]:
        stored = review["bags"][bag_id]
        current_scan = latest.get(bag_id)
        current_scan_snapshot = (
            None
            if current_scan is None
            else _scan_snapshot(current_scan["scan_id"], current_scan["entry"])
        )
        if current_scan_snapshot != stored["last_scan"]:
            changes.append(
                {
                    "kind": "last_scan",
                    "bag_id": bag_id,
                    "recorded": stored["last_scan"],
                    "current": current_scan_snapshot,
                }
            )
        current_clearance = _current_clearance_record(state, bag_id)
        current_clearance_snapshot = _clearance_snapshot(current_clearance)
        if current_clearance_snapshot != stored["clearance"]:
            changes.append(
                {
                    "kind": "clearance",
                    "bag_id": bag_id,
                    "recorded": stored["clearance"],
                    "current": current_clearance_snapshot,
                }
            )

        occupied_by = occupied.get(bag_id, [])
        flight_record = flights[flight_id]["record"] if flight_id in flights else None
        if flight_record is None:
            reasons = ["not_assigned_to_flight", "occupied"] if occupied_by else [
                "not_assigned_to_flight"
            ]
            reasons = _reasons(reasons)
        else:
            reasons = _evaluate(
                bags[bag_id],
                flight_id,
                flight_record,
                current_scan,
                current_clearance,
                occupied_by,
            )
        rows[bag_id] = {
            "eligible": not reasons,
            "unmet_conditions": reasons,
            "occupied_by": occupied_by,
            "last_scan": current_scan_snapshot,
            "clearance": current_clearance_snapshot,
        }
    return changes, rows


def _commit_receipt(manifest):
    return json.loads(json.dumps(manifest["receipt"]))


def commit_review(archive_path, review_id):
    """Register a whole committable review atomically and claim its bags."""
    with archive._locked(archive_path):
        view = _load_all(archive_path)
        state = view["state"]
        review = state["reviews"].get(review_id)
        if review is None:
            raise ValueError(f"unknown review {review_id}")

        # A successful commit is idempotent: retries return the first receipt,
        # even after a later cancellation or screening revocation.
        committed_manifest_id = review.get("committed_manifest_id")
        if committed_manifest_id is not None:
            manifest = state["manifests"].get(committed_manifest_id)
            if manifest is not None:
                return _commit_receipt(manifest)
            raise ValueError(f"review {review_id} references a missing manifest")

        manifest_id = review["manifest_id"]
        if manifest_id in state["manifests"]:
            raise ValueError(
                f"manifest_id {manifest_id} has already been registered or cancelled"
            )
        if not review.get("committable"):
            raise ValueError(f"review {review_id} was not committable at review time")

        changes, rows = _evidence_changes(review, view, exclude_manifest=None)
        if changes:
            kinds = ", ".join(sorted({change["kind"] for change in changes}))
            raise ValueError(
                f"review {review_id} is stale: relevant evidence changed ({kinds})"
            )
        blocked = {
            bag_id: row["unmet_conditions"]
            for bag_id, row in rows.items()
            if not row["eligible"]
        }
        if blocked:
            detail = "; ".join(f"{bag}: {', '.join(reasons)}" for bag, reasons in blocked.items())
            raise ValueError(f"review {review_id} can no longer be committed: {detail}")

        receipt = {
            "manifest_id": manifest_id,
            "review_id": review_id,
            "flight_id": review["flight_id"],
            "bag_ids": list(review["requested_bag_ids"]),
            "committed": True,
            "archive_knowledge_seq": view["as_of"],
        }
        manifest = {
            "manifest_id": manifest_id,
            "review_id": review_id,
            "flight_id": review["flight_id"],
            "bag_ids": list(review["requested_bag_ids"]),
            "committed_at_knowledge_seq": view["as_of"],
            "review_snapshot": review,
            "receipt": receipt,
            "cancelled": False,
        }
        # The manifest (and therefore every bag claim), the review's commit
        # marker and the receipt all land in one atomic file replacement.
        state["manifests"][manifest_id] = manifest
        state["reviews"][review_id] = {**review, "committed_manifest_id": manifest_id}
        _save_workbench(archive_path, state)
        return _commit_receipt(manifest)


# ---------------------------------------------------------------------------
# 5. cancel
# ---------------------------------------------------------------------------


def cancel_manifest(archive_path, manifest_id):
    """Release every bag claimed by a manifest; repeating is a no-op."""
    with archive._locked(archive_path):
        view = _load_all(archive_path)
        state = view["state"]
        manifest = state["manifests"].get(manifest_id)
        if manifest is None:
            raise ValueError(f"unknown manifest {manifest_id}")
        already_cancelled = manifest["cancelled"]
        if not already_cancelled:
            state["manifests"][manifest_id] = {**manifest, "cancelled": True}
            _save_workbench(archive_path, state)
        return {
            "manifest_id": manifest_id,
            "review_id": manifest["review_id"],
            "bag_ids": list(manifest["bag_ids"]),
            "status": "cancelled",
            "already_cancelled": already_cancelled,
        }


# ---------------------------------------------------------------------------
# 6. status
# ---------------------------------------------------------------------------


def manifest_status(archive_path, manifest_id):
    """Return the recorded basis plus a fresh re-check against current evidence."""
    with archive._locked(archive_path):
        view = _load_all(archive_path)
        state = view["state"]
        manifest = state["manifests"].get(manifest_id)
        if manifest is None:
            raise ValueError(f"unknown manifest {manifest_id}")
        review = manifest["review_snapshot"]
        changes, rows = _evidence_changes(
            review, view, exclude_manifest=manifest_id
        )
        unmet = {
            bag_id: row["unmet_conditions"]
            for bag_id, row in rows.items()
            if not row["eligible"]
        }
        if manifest["cancelled"]:
            status = "cancelled"
        elif changes or unmet:
            status = "suspended"
        else:
            status = "active"
        return {
            "manifest_id": manifest_id,
            "review_id": manifest["review_id"],
            "flight_id": manifest["flight_id"],
            "bag_ids": list(manifest["bag_ids"]),
            "status": status,
            "original_basis": {
                "review_id": review["review_id"],
                "manifest_id": review["manifest_id"],
                "flight_id": review["flight_id"],
                "flight_source": review["flight_source"],
                "archive_knowledge_seq": review["archive_knowledge_seq"],
                "bags": review["bags"],
                "committable": review["committable"],
            },
            "registration": {
                "committed": True,
                "cancelled": manifest["cancelled"],
                "committed_at_knowledge_seq": manifest["committed_at_knowledge_seq"],
            },
            "current": {
                "checked_against_knowledge_seq": view["as_of"],
                "evidence_changed": bool(changes),
                "changed_sources": changes,
                "unmet_conditions": unmet,
                "bags": rows,
            },
        }

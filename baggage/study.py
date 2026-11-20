"""Local sample-release workspace and contamination tracking.

The study workspace lives **beside** an existing verification archive.  It
registers manual ``success``/``failure`` labels and manually declared
case links for bags that already exist in the archive, publishes named,
frozen sample releases, and answers -- for any of its own ``study_rev``
snapshots -- which released rows are still safe to use and which must be
withdrawn.

It records manual evidence only.  It never treats the archive's verification
or a published row as ground truth, never trains a model, never contacts an
external service, and never drives scheduling or loading.  The archive's
``events.jsonl`` stays read-only.

State is one JSON document, ``<archive>/study-workbench.json``, created by
``study-init`` and replaced atomically (temp file, fsync, ``os.replace``) under
the archive-wide advisory lock.  A command either commits its whole batch,
release and revision bump or leaves the previous document byte-for-byte
intact.

Python API (standard library only)::

    init_study(archive_path)
    apply_study(archive_path, batch_path)
    publish_release(archive_path, request_path)
    view_study(archive_path, release_id=None, as_of=None)
"""

import json
import os
import time
from collections import deque
from pathlib import Path

from . import archive
from .core import reconcile

STUDY_FILE = "study-workbench.json"
STUDY_VERSION = 1

RESULTS = ("success", "failure")
PARTITIONS = ("train", "eval")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _require_str(record, key, label):
    value = record.get(key) if isinstance(record, dict) else None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} requires a non-empty string field {key!r}")
    return value


def _canonical_pair(a, b):
    return (a, b) if a < b else (b, a)


def _pair_key(pair):
    return f"{pair[0]}\x00{pair[1]}"


def _pair_from_key(key):
    a, b = key.split("\x00")
    return a, b


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path, payload):
    """Replace ``path`` atomically so readers see the old or the new state."""
    target = Path(path)
    temp = target.parent / f".{target.name}.tmp-{os.getpid()}-{time.time_ns()}"
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
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


# ---------------------------------------------------------------------------
# durable state
# ---------------------------------------------------------------------------


def _study_path(archive_path):
    return Path(archive_path) / STUDY_FILE


def _empty_state():
    return {
        "version": STUDY_VERSION,
        "study_rev": 0,
        "batches": [],
        "label_versions": [],
        "link_events": [],
        "releases": {},
    }


def _load_study(archive_path):
    path = _study_path(archive_path)
    if not path.is_file():
        raise ValueError(f"study workbench does not exist for archive {archive_path}")
    try:
        state = archive._load_json(path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"corrupt study workbench: {exc}") from exc
    if not isinstance(state, dict) or state.get("version") != STUDY_VERSION:
        raise ValueError("corrupt study workbench: unsupported layout")
    if not isinstance(state.get("study_rev"), int) or isinstance(state.get("study_rev"), bool):
        raise ValueError("corrupt study workbench: malformed study revision")
    for key in ("batches", "label_versions", "link_events"):
        if not isinstance(state.get(key), list):
            raise ValueError("corrupt study workbench: malformed state")
    if not isinstance(state.get("releases"), dict):
        raise ValueError("corrupt study workbench: malformed releases")
    return state


def _save_study(archive_path, state):
    _atomic_write(_study_path(archive_path), state)


def _archive_view(archive_path):
    """Return the event log, its latest accepted sequence, and the bag ids."""
    events = archive._load_events(archive_path)
    bags = events[0]["bags"]
    return events, events[-1]["knowledge_seq"], {bag["id"] for bag in bags}


# ---------------------------------------------------------------------------
# historical projections
# ---------------------------------------------------------------------------


def _snapshot(state, rev, bag_ids):
    """Project labels, links, exposures and groups as they stood at ``rev``."""
    labels = {}
    for item in state["label_versions"]:
        if item["study_rev"] <= rev:
            labels[item["bag_id"]] = item

    links = {}
    for item in state["link_events"]:
        if item["study_rev"] > rev:
            continue
        key = _pair_key(_canonical_pair(item["a"], item["b"]))
        if item["op"] == "link":
            links[key] = item
        else:
            links.pop(key, None)

    exposures = []
    for release_id, entry in state["releases"].items():
        release = entry["release"]
        if release["study_rev"] > rev:
            continue
        for row in release["rows"]:
            exposures.append(
                {
                    "bag_id": row["bag_id"],
                    "partition": row["partition"],
                    "release_id": release_id,
                    "study_rev": release["study_rev"],
                }
            )

    adjacency = {bag_id: set() for bag_id in bag_ids}
    for key in links:
        a, b = _pair_from_key(key)
        adjacency[a].add(b)
        adjacency[b].add(a)

    components = {}
    for bag_id in sorted(bag_ids):
        if bag_id in components:
            continue
        seen = {bag_id}
        queue = deque([bag_id])
        while queue:
            node = queue.popleft()
            for neighbour in sorted(adjacency[node]):
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append(neighbour)
        group = frozenset(seen)
        for member in group:
            components[member] = group

    return {
        "rev": rev,
        "labels": labels,
        "links": links,
        "exposures": exposures,
        "adjacency": adjacency,
        "components": components,
    }


def _component_exposures(snap, component):
    return [entry for entry in snap["exposures"] if entry["bag_id"] in component]


def _paths(snap, start):
    """Deterministic BFS predecessor map from ``start`` over current links."""
    previous = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for neighbour in sorted(snap["adjacency"][node]):
            if neighbour not in previous:
                previous[neighbour] = node
                queue.append(neighbour)
    return previous


def _chain(snap, start, goal):
    """A deterministic shortest link chain from ``start`` to ``goal``."""
    previous = _paths(snap, start)
    if goal not in previous:
        return None
    nodes = []
    node = goal
    while node is not None:
        nodes.append(node)
        node = previous[node]
    nodes.reverse()
    edges = []
    for left, right in zip(nodes, nodes[1:]):
        key = _pair_key(_canonical_pair(left, right))
        event = snap["links"][key]
        edges.append(
            {
                # Path order (never the stored canonical order) so a reviewer
                # can follow the chain in one direction.
                "bags": [left, right],
                "study_rev": event["study_rev"],
                "batch_id": event["batch_id"],
            }
        )
    return edges


# ---------------------------------------------------------------------------
# frozen archive features
# ---------------------------------------------------------------------------


def _feature_snapshot(events, bag_id, as_of, cache):
    """Return the archive features and their source versions at ``as_of``."""
    if as_of not in cache:
        bags, scans, flights = archive._project(events, as_of)
        report = reconcile(
            {
                "flights": [entry["record"] for entry in flights.values()],
                "bags": bags,
                "scans": [entry["record"] for entry in scans.values()],
            }
        )
        cache[as_of] = (
            {row["bag_id"]: row for row in report["bags"]},
            scans,
            flights,
            {bag["id"]: bag for bag in bags},
        )
    rows, scans, flights, bags_by_id = cache[as_of]
    row = rows[bag_id]
    bag = bags_by_id[bag_id]

    last_scan = row["last_scan"]
    if last_scan is None:
        scan_source = None
    else:
        entry = scans[last_scan]
        scan_source = {
            "scan_id": last_scan,
            "knowledge_seq": entry["knowledge_seq"],
            "batch_id": entry["batch_id"],
        }

    def flight_source(flight_id):
        entry = flights[flight_id]
        return {
            "flight_id": flight_id,
            "knowledge_seq": entry["knowledge_seq"],
            "batch_id": entry["batch_id"],
        }

    next_flight = bag.get("next_flight_id")
    features = {
        "status": row["status"],
        "last_scan": last_scan,
        "connection_at_risk": row["connection_at_risk"],
    }
    sources = {
        "last_scan": scan_source,
        "flights": {
            "current": flight_source(bag["flight_id"]),
            "next": flight_source(next_flight) if next_flight else None,
        },
    }
    return features, sources


# ---------------------------------------------------------------------------
# 1. initialise
# ---------------------------------------------------------------------------


def init_study(archive_path):
    """Create the single study workbench beside an existing valid archive."""
    with archive._locked(archive_path):
        archive._load_events(archive_path)
        path = _study_path(archive_path)
        if path.exists():
            raise ValueError(f"study workbench already exists for archive {archive_path}")
        _save_study(archive_path, _empty_state())
    return {"archive": str(archive_path), "workbench": str(path), "study_rev": 0}


# ---------------------------------------------------------------------------
# 2. label and link batches
# ---------------------------------------------------------------------------


def _validate_label_operation(operation, bag_ids, labels, max_seq, seen_bags):
    if not isinstance(operation, dict):
        raise ValueError("label operation must be a JSON object")
    bag_id = _require_str(operation, "bag_id", "label operation")
    if bag_id in seen_bags:
        raise ValueError(f"bag {bag_id} has more than one label operation in the same batch")
    seen_bags.add(bag_id)
    if bag_id not in bag_ids:
        raise ValueError(f"label operation references unknown bag {bag_id}")
    if "result" not in operation:
        raise ValueError(f"label operation for bag {bag_id} requires a result field")
    result = operation["result"]
    if result is None:
        # A retraction names only the bag; any extra value is ignored and the
        # retraction itself carries no feature cutoff.
        current = labels.get(bag_id)
        if current is None or current["result"] is None:
            raise ValueError(f"bag {bag_id} has no valid label to retract")
        return {"bag_id": bag_id, "result": None}
    if not isinstance(result, str) or result not in RESULTS:
        raise ValueError(f"label result for bag {bag_id} must be 'success' or 'failure'")
    as_of = operation.get("features_as_of")
    if not isinstance(as_of, int) or isinstance(as_of, bool) or as_of < 1 or as_of > max_seq:
        raise ValueError(
            f"label features_as_of for bag {bag_id} must be an accepted knowledge_seq "
            f"between 1 and {max_seq}"
        )
    return {"bag_id": bag_id, "result": result, "features_as_of": as_of}


def _validate_link_operation(operation, bag_ids, links, seen_pairs):
    if not isinstance(operation, dict):
        raise ValueError("link operation must be a JSON object")
    op = operation.get("op")
    if op not in ("link", "unlink"):
        raise ValueError(f"unknown link op {op!r}")
    pair = operation.get("bags")
    if not isinstance(pair, list) or len(pair) != 2:
        raise ValueError("link operation bags must be a list of exactly two bag ids")
    left, right = pair
    for value in (left, right):
        if not isinstance(value, str) or not value:
            raise ValueError("link operation bags must be non-empty strings")
    if left == right:
        raise ValueError("a link must join two different bags")
    for value in (left, right):
        if value not in bag_ids:
            raise ValueError(f"link operation references unknown bag {value}")
    key = _canonical_pair(left, right)
    if key in seen_pairs:
        raise ValueError(f"bags {key[0]} and {key[1]} appear more than once in the same batch")
    seen_pairs.add(key)
    exists = _pair_key(key) in links
    if op == "link" and exists:
        raise ValueError(f"bags {key[0]} and {key[1]} are already linked")
    if op == "unlink" and not exists:
        raise ValueError(f"bags {key[0]} and {key[1]} are not linked")
    return {"op": op, "a": key[0], "b": key[1]}


def _validate_study_batch(batch, bag_ids, state, max_seq):
    if not isinstance(batch, dict):
        raise ValueError("study batch must be a JSON object")
    batch_id = _require_str(batch, "batch_id", "study batch")
    if any(entry["batch_id"] == batch_id for entry in state["batches"]):
        raise ValueError(f"duplicate study batch_id {batch_id}")
    raw_labels = batch.get("labels", [])
    raw_links = batch.get("links", [])
    if not isinstance(raw_labels, list) or not isinstance(raw_links, list):
        raise ValueError("study batch labels and links must be lists")
    if not raw_labels and not raw_links:
        raise ValueError("study batch must contain at least one label or link operation")

    current = _snapshot(state, state["study_rev"], bag_ids)
    labels = []
    seen_bags = set()
    for operation in raw_labels:
        labels.append(
            _validate_label_operation(
                operation, bag_ids, current["labels"], max_seq, seen_bags
            )
        )
    links = []
    seen_pairs = set()
    for operation in raw_links:
        links.append(_validate_link_operation(operation, bag_ids, current["links"], seen_pairs))
    return batch_id, labels, links


def apply_study(archive_path, batch_path):
    """Accept one whole label/link batch or none of it; bump ``study_rev`` once."""
    with archive._locked(archive_path):
        state = _load_study(archive_path)
        _events, max_seq, bag_ids = _archive_view(archive_path)
        batch = archive._load_json(batch_path)
        batch_id, labels, links = _validate_study_batch(batch, bag_ids, state, max_seq)

        new_rev = state["study_rev"] + 1
        for label in sorted(labels, key=lambda item: item["bag_id"]):
            record = {
                "bag_id": label["bag_id"],
                "result": label["result"],
                "study_rev": new_rev,
                "batch_id": batch_id,
            }
            if label["result"] is not None:
                record["features_as_of"] = label["features_as_of"]
            state["label_versions"].append(record)
        for link in sorted(links, key=lambda item: (item["a"], item["b"])):
            state["link_events"].append(
                {
                    "op": link["op"],
                    "a": link["a"],
                    "b": link["b"],
                    "study_rev": new_rev,
                    "batch_id": batch_id,
                }
            )
        state["batches"].append({"batch_id": batch_id, "study_rev": new_rev})
        state["study_rev"] = new_rev
        _save_study(archive_path, state)
        return {
            "batch_id": batch_id,
            "accepted": True,
            "study_rev": new_rev,
            "archive_knowledge_seq": max_seq,
            "labels": labels,
            "links": links,
        }


# ---------------------------------------------------------------------------
# 3. publish
# ---------------------------------------------------------------------------


def _parse_request(request):
    """Validate the request shape and return ``(release_id, sorted rows)``."""
    if not isinstance(request, dict):
        raise ValueError("release request must be a JSON object")
    release_id = _require_str(request, "release_id", "release request")
    raw_rows = request.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError("release request rows must be a non-empty list")
    rows = []
    seen = set()
    for row in raw_rows:
        if not isinstance(row, dict):
            raise ValueError("release row must be a JSON object")
        bag_id = _require_str(row, "bag_id", "release row")
        partition = row.get("partition")
        if partition not in PARTITIONS:
            raise ValueError(f"release row for bag {bag_id} partition must be 'train' or 'eval'")
        if bag_id in seen:
            raise ValueError(f"release request repeats bag {bag_id}")
        seen.add(bag_id)
        rows.append({"bag_id": bag_id, "partition": partition})
    return release_id, sorted(rows, key=lambda item: item["bag_id"])


def _check_partitions(rows, snap):
    """Reject any group that would be split or is already contaminated."""
    per_group = {}
    for row in rows:
        group = snap["components"][row["bag_id"]]
        per_group.setdefault(group, set()).add(row["partition"])
    for group in sorted(per_group, key=lambda item: sorted(item)):
        existing = {
            entry["partition"] for entry in _component_exposures(snap, group)
        }
        new = per_group[group]
        members = ", ".join(sorted(group))
        if len(existing) > 1:
            raise ValueError(
                f"group [{members}] is already exposed to both train and eval; "
                "no part of it may be released"
            )
        if len(new) > 1:
            raise ValueError(
                f"group [{members}] cannot be sent to both train and eval by one release"
            )
        if existing and new != existing:
            raise ValueError(
                f"group [{members}] is already exposed to {sorted(existing)[0]} "
                f"and may not be released to {sorted(new)[0]}"
            )


def publish_release(archive_path, request_path):
    """Freeze one whole release or reject it; bump ``study_rev`` once."""
    with archive._locked(archive_path):
        state = _load_study(archive_path)
        events, max_seq, bag_ids = _archive_view(archive_path)
        request = archive._load_json(request_path)
        release_id, rows = _parse_request(request)

        existing = state["releases"].get(release_id)
        if existing is not None:
            if existing["request"] == rows:
                return json.loads(json.dumps(existing["release"]))
            raise ValueError(
                f"release_id {release_id} was already published with different content"
            )

        for row in rows:
            if row["bag_id"] not in bag_ids:
                raise ValueError(f"release row references unknown bag {row['bag_id']}")
        label_snap = _snapshot(state, state["study_rev"], bag_ids)
        for row in rows:
            label = label_snap["labels"].get(row["bag_id"])
            if label is None or label["result"] is None:
                raise ValueError(f"release row for bag {row['bag_id']} has no valid label")
        _check_partitions(rows, label_snap)

        new_rev = state["study_rev"] + 1
        cache = {}
        frozen_rows = []
        for row in rows:
            label = label_snap["labels"][row["bag_id"]]
            features, sources = _feature_snapshot(
                events, row["bag_id"], label["features_as_of"], cache
            )
            frozen_rows.append(
                {
                    "bag_id": row["bag_id"],
                    "partition": row["partition"],
                    "label": {
                        "bag_id": row["bag_id"],
                        "result": label["result"],
                        "features_as_of": label["features_as_of"],
                        "study_rev": label["study_rev"],
                        "batch_id": label["batch_id"],
                    },
                    "features": features,
                    "sources": sources,
                }
            )

        release = {
            "release_id": release_id,
            "study_rev": new_rev,
            "archive_knowledge_seq": max_seq,
            "rows": frozen_rows,
        }
        state["releases"][release_id] = {
            "release": release,
            "request": rows,
        }
        state["study_rev"] = new_rev
        _save_study(archive_path, state)
        return json.loads(json.dumps(release))


# ---------------------------------------------------------------------------
# 4. view availability
# ---------------------------------------------------------------------------


def _contamination_evidence(bag_id, group, snap):
    picked = {}
    for partition in PARTITIONS:
        candidates = sorted(
            (entry for entry in _component_exposures(snap, group)
             if entry["partition"] == partition),
            # Prefer a different bag so the chain shows the connection, but stay
            # deterministic when the checked bag is itself the only exposure.
            key=lambda entry: (
                entry["bag_id"] == bag_id,
                entry["bag_id"],
                entry["release_id"],
                entry["study_rev"],
            ),
        )
        if not candidates:
            continue
        exposure = dict(candidates[0])
        picked[partition] = {
            "exposure": exposure,
            "chain": _chain(snap, bag_id, exposure["bag_id"]),
        }
    return {
        "checked_bag_id": bag_id,
        "group": sorted(group),
        "exposures": picked,
    }


def _row_availability(row, snap):
    bag_id = row["bag_id"]
    reasons = []
    current = snap["labels"].get(bag_id)
    frozen_rev = row["label"]["study_rev"]
    label_ok = (
        current is not None
        and current["result"] is not None
        and current["study_rev"] == frozen_rev
    )
    if not label_ok:
        reasons.append("label_version_changed")

    group = snap["components"][bag_id]
    group_exposures = _component_exposures(snap, group)
    partitions = {entry["partition"] for entry in group_exposures}
    evidence = None
    if "train" in partitions and "eval" in partitions:
        reasons.append("group_exposure_conflict")
        evidence = _contamination_evidence(bag_id, group, snap)

    current_label = None
    if current is not None:
        current_label = {
            "result": current["result"],
            "study_rev": current["study_rev"],
            "batch_id": current["batch_id"],
            "features_as_of": current.get("features_as_of"),
        }

    return {
        "bag_id": bag_id,
        "partition": row["partition"],
        "usable": not reasons,
        "withdrawn_reasons": reasons,
        "label": {
            "frozen": dict(row["label"]),
            "current": current_label,
        },
        "group": {
            "bags": sorted(group),
            "exposure_partitions": sorted(partitions),
            "exposures": [
                {
                    "bag_id": entry["bag_id"],
                    "partition": entry["partition"],
                    "release_id": entry["release_id"],
                    "study_rev": entry["study_rev"],
                }
                for entry in sorted(
                    group_exposures,
                    key=lambda item: (
                        item["partition"],
                        item["bag_id"],
                        item["release_id"],
                    ),
                )
            ],
        },
        "evidence": evidence,
    }


def view_study(archive_path, release_id=None, as_of=None):
    """Report released rows and their usability at one ``study_rev``."""
    with archive._locked(archive_path):
        state = _load_study(archive_path)
        _events, _max_seq, bag_ids = _archive_view(archive_path)
        current_rev = state["study_rev"]
        if as_of is None:
            rev = current_rev
        else:
            if not isinstance(as_of, int) or isinstance(as_of, bool) or as_of < 0:
                raise ValueError(f"unknown study revision {as_of!r}")
            if as_of > current_rev:
                raise ValueError(f"unknown study revision {as_of}")
            rev = as_of

        snap = _snapshot(state, rev, bag_ids)
        if release_id is not None:
            entry = state["releases"].get(release_id)
            if entry is None:
                raise ValueError(f"unknown release {release_id}")
            if entry["release"]["study_rev"] > rev:
                raise ValueError(
                    f"release {release_id} did not exist at study revision {rev}"
                )
            selected = [entry]
        else:
            selected = [
                entry
                for _rid, entry in sorted(state["releases"].items())
                if entry["release"]["study_rev"] <= rev
            ]

        releases = []
        for entry in selected:
            release = entry["release"]
            releases.append(
                {
                    "release_id": release["release_id"],
                    "published_study_rev": release["study_rev"],
                    "frozen": json.loads(json.dumps(release)),
                    "availability": {
                        "checked_at_study_rev": rev,
                        "rows": [_row_availability(row, snap) for row in release["rows"]],
                    },
                }
            )

        result = {
            "study_rev": rev,
            "current_study_rev": current_rev,
            "release_id": release_id,
            "releases": releases,
        }
        return json.loads(json.dumps(result))

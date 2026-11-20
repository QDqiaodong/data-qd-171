"""Local read-audit reconciliation for one transfer station.

A transfer desk occasionally receives a batch of imperfect *reads*: the device
did capture something, but the barcode was only partly decoded and upstream
could offer several possible bag ids.  For one station and one archive cutoff
this module answers how many bags that must be checked can be given ownership
evidence by this batch of reads, which bags are certainly missing evidence, and
which reads still need a human to look at the original image.

It is a pure, read-only local calculation:

* it never writes to the archive and keeps no state between runs;
* it does not scan or resolve the archive's old scans or connection risk;
* a match is *ownership evidence for this reconciliation round only* -- it does
  not prove a bag is physically present and does not decide loading.

The optimisation is maximum bipartite matching (read -> bag), with optional
human decisions acting as hard constraints.  Maximum matching alone is not
enough for the report: the Dulmage-Mendelsohn decomposition of the residual
alternating graph tells us, for every bag and every read, what is true across
*all* maximum-coverage schemes rather than in the one example we print.

Python API (standard library only)::

    build_report(archive_path, as_of, batch_path, decisions_path=None)
    write_report(archive_path, as_of, batch_path, output_path,
                 decisions_path=None)
"""

import json
import os
import time
from collections import deque
from pathlib import Path

from . import archive
from .core import _time

REPORT_FORMAT = "bag-reconcile-read-audit/v1"

# Bag evidence conclusions.
EVIDENCE_CERTAIN = "certain_evidence"
EVIDENCE_POSSIBLE = "possible_missing"
EVIDENCE_MISSING = "certain_missing"

# Per-read outcomes.
STATUS_CONFIRMED = "confirmed"
STATUS_DISCARDED = "discarded"
STATUS_OUTSIDE = "outside_capture_window"
STATUS_ALWAYS_USED = "always_used"
STATUS_OPTIONAL = "optional"
STATUS_UNUSED = "unused"

# Candidate classification for reads in the capture window.
CANDIDATE_IN_SCOPE = "in_scope"
CANDIDATE_UNKNOWN = "unknown_bag"
CANDIDATE_OUT_OF_SCOPE = "out_of_scope"

PAIR_MANUAL = "manual_confirmation"
PAIR_RECONCILED = "reconciliation"

BATCH_KEYS = {"station", "departure_from", "departure_until",
              "capture_from", "capture_until", "reads"}
READ_KEYS = {"read_id", "captured_at", "candidate_bag_ids"}
DECISION_KEYS = {"confirm", "discard"}
CONFIRM_KEYS = {"read_id", "bag_id", "proof_ref"}
DISCARD_KEYS = {"read_id", "proof_ref"}


# ---------------------------------------------------------------------------
# strict JSON helpers
# ---------------------------------------------------------------------------


def _require_object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _require_list(value, label):
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _require_str(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _exact_keys(record, keys, label):
    extra = set(record) - keys
    missing = keys - set(record)
    if extra:
        raise ValueError(f"{label} has unknown field {sorted(extra)[0]!r}")
    if missing:
        raise ValueError(f"{label} is missing field {sorted(missing)[0]!r}")


def _timestamp(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty timestamp string")
    return _time(value)


# ---------------------------------------------------------------------------
# batch input
# ---------------------------------------------------------------------------


def _parse_batch(data):
    """Validate the strict BATCH document and normalise its values."""
    _require_object(data, "batch")
    _exact_keys(data, BATCH_KEYS, "batch")
    station = _require_str(data["station"], "batch station")
    departure_from = _timestamp(data["departure_from"], "batch departure_from")
    departure_until = _timestamp(data["departure_until"], "batch departure_until")
    capture_from = _timestamp(data["capture_from"], "batch capture_from")
    capture_until = _timestamp(data["capture_until"], "batch capture_until")
    if not departure_from < departure_until:
        raise ValueError("departure window start must be strictly before its end")
    if not capture_from < capture_until:
        raise ValueError("capture window start must be strictly before its end")

    raw_reads = _require_list(data["reads"], "batch reads")
    reads = []
    seen = set()
    for position, read in enumerate(raw_reads):
        label = f"read[{position}]"
        _require_object(read, label)
        _exact_keys(read, READ_KEYS, label)
        read_id = _require_str(read["read_id"], f"{label} read_id")
        if read_id in seen:
            raise ValueError(f"duplicate read_id {read_id}")
        seen.add(read_id)
        captured_at = _timestamp(read["captured_at"], f"read {read_id} captured_at")
        raw_candidates = _require_list(
            read["candidate_bag_ids"], f"read {read_id} candidate_bag_ids"
        )
        candidates = []
        candidate_seen = set()
        for raw in raw_candidates:
            candidate = _require_str(raw, f"read {read_id} candidate bag_id")
            if candidate in candidate_seen:
                raise ValueError(
                    f"read {read_id} lists candidate {candidate} more than once"
                )
            candidate_seen.add(candidate)
            candidates.append(candidate)
        reads.append(
            {
                "read_id": read_id,
                "captured_at": read["captured_at"],
                "instant": captured_at,
                "candidates": candidates,
            }
        )
    return {
        "station": station,
        "departure_from_text": data["departure_from"],
        "departure_until_text": data["departure_until"],
        "capture_from_text": data["capture_from"],
        "capture_until_text": data["capture_until"],
        "departure_from": departure_from,
        "departure_until": departure_until,
        "capture_from": capture_from,
        "capture_until": capture_until,
        "reads": reads,
    }


# ---------------------------------------------------------------------------
# human decisions input
# ---------------------------------------------------------------------------


def _parse_decisions(data):
    """Validate the strict DECISIONS document's structure.

    Cross-references to reads and bags (window membership, in-scope
    candidates, one-decision-per-read, one-confirmation-per-bag) are checked
    later, once the archive scope for this round is known.  Any conflict there
    rejects the whole request rather than keeping a first entry.
    """
    _require_object(data, "decisions")
    extra = set(data) - DECISION_KEYS
    if extra:
        raise ValueError(f"decisions has unknown field {sorted(extra)[0]!r}")
    confirms = []
    discards = []
    for field, target, keys, label in (
        ("confirm", confirms, CONFIRM_KEYS, "confirm"),
        ("discard", discards, DISCARD_KEYS, "discard"),
    ):
        entries = data.get(field, [])
        _require_list(entries, f"decisions {field}")
        for position, entry in enumerate(entries):
            item_label = f"decisions {field}[{position}]"
            _require_object(entry, item_label)
            _exact_keys(entry, keys, item_label)
            parsed = {
                "read_id": _require_str(entry["read_id"], f"{item_label} read_id"),
                "proof_ref": _require_str(entry["proof_ref"], f"{item_label} proof_ref"),
            }
            if field == "confirm":
                parsed["bag_id"] = _require_str(entry["bag_id"], f"{item_label} bag_id")
                confirms.append(parsed)
            else:
                discards.append(parsed)
    return {"confirm": confirms, "discard": discards}


def _validate_decisions(decisions, window_reads, in_scope_candidates):
    """Resolve decisions against reads and candidates, or reject them all.

    ``window_reads`` maps ``read_id`` to the in-window read.  ``in_scope_candidates``
    maps ``read_id`` to the list of candidate bag ids that are actually
    checkable in this round (known, next leg from the station, departure in the
    window).  A read that is absent (unknown or out of the capture window) and
    a bag that is not one of a read's checkable candidates are both rejected.
    """
    seen_reads = {}
    confirmed_bags = {}
    resolved = {}
    for entry in decisions["confirm"]:
        read_id = entry["read_id"]
        if read_id in seen_reads:
            raise ValueError(
                f"read {read_id} has more than one decision "
                f"({seen_reads[read_id]} and confirm)"
            )
        if read_id not in window_reads:
            raise ValueError(
                f"confirm references read {read_id} which is not in the capture window"
            )
        bag_id = entry["bag_id"]
        if bag_id not in in_scope_candidates.get(read_id, set()):
            raise ValueError(
                f"confirm for read {read_id} names {bag_id}, which is not one of "
                "its checkable candidates"
            )
        if bag_id in confirmed_bags:
            raise ValueError(
                f"bag {bag_id} is confirmed by both {confirmed_bags[bag_id]} and {read_id}"
            )
        seen_reads[read_id] = "confirm"
        confirmed_bags[bag_id] = read_id
        resolved[read_id] = {
            "action": "confirm",
            "bag_id": bag_id,
            "proof_ref": entry["proof_ref"],
        }
    for entry in decisions["discard"]:
        read_id = entry["read_id"]
        if read_id in seen_reads:
            raise ValueError(
                f"read {read_id} has more than one decision "
                f"({seen_reads[read_id]} and discard)"
            )
        if read_id not in window_reads:
            raise ValueError(
                f"discard references read {read_id} which is not in the capture window"
            )
        seen_reads[read_id] = "discard"
        resolved[read_id] = {"action": "discard", "proof_ref": entry["proof_ref"]}
    return resolved


# ---------------------------------------------------------------------------
# archive scope
# ---------------------------------------------------------------------------


def _cutoff(events, as_of):
    max_seq = events[-1]["knowledge_seq"]
    if (
        not isinstance(as_of, int)
        or isinstance(as_of, bool)
        or as_of < 1
        or as_of > max_seq
    ):
        raise ValueError(f"unknown or unaccepted knowledge_seq: {as_of}")
    return as_of


def _scope_bags(bags, flights, station, window_start, window_end):
    """Bags that this round must reconcile, keyed by bag_id.

    A bag is in scope when it has a next leg, that leg leaves from ``station``,
    and its scheduled departure lies in the half-open departure window.  The
    archive's existing scans and connection risk never participate.
    """
    in_scope = {}
    for bag in bags:
        next_flight_id = bag.get("next_flight_id")
        if not next_flight_id:
            continue
        flight = flights.get(next_flight_id)
        if flight is None:
            continue
        record = flight["record"]
        if record["origin"] != station:
            continue
        departure = _time(record["departure"])
        if not (window_start <= departure < window_end):
            continue
        in_scope[bag["id"]] = {
            "bag_id": bag["id"],
            "next_flight_id": next_flight_id,
            "flight": {
                "flight_id": next_flight_id,
                "origin": record["origin"],
                "destination": record["destination"],
                "departure": record["departure"],
                "arrival": record["arrival"],
            },
            "source": {
                "knowledge_seq": flight["knowledge_seq"],
                "batch_id": flight["batch_id"],
            },
        }
    return in_scope



# ---------------------------------------------------------------------------
# maximum matching and "all maximum schemes" analysis
# ---------------------------------------------------------------------------


class _Matcher:
    """Hopcroft-Karp matching over an integer-indexed bipartite graph.

    Reads and bags are addressed by dense indices for speed on the largest
    batches.  ``adj[left]`` is the sorted list of right indices.  Human
    decisions are applied by fixing those edges before augmenting (a confirmed
    read has one edge and is pre-matched; a discarded read has none), so every
    maximum matching found respects them automatically.  Both the layering BFS
    and the augmenting DFS are iterative, so a 4,500-vertex path can never hit
    Python's recursion limit.
    """

    __slots__ = ("n_left", "n_right", "adj", "match_left", "match_right")

    def __init__(self, n_left, n_right, adj):
        self.n_left = n_left
        self.n_right = n_right
        self.adj = adj
        self.match_left = [-1] * n_left
        self.match_right = [-1] * n_right

    def force(self, left, right):
        self.match_left[left] = right
        self.match_right[right] = left

    def _augment(self, root, dist):
        match_right = self.match_right
        match_left = self.match_left
        adj = self.adj
        stack = [root]
        iters = {root: 0}
        chosen = {}
        visited = {root}
        while stack:
            left = stack[-1]
            options = adj[left]
            pos = iters[left]
            advanced = False
            while pos < len(options):
                right = options[pos]
                pos += 1
                nxt = match_right[right]
                if nxt == -1:
                    iters[left] = pos
                    chosen[left] = right
                    for depth in range(len(stack) - 1, -1, -1):
                        node = stack[depth]
                        edge = chosen[node]
                        match_left[node] = edge
                        match_right[edge] = node
                    return True
                if dist[nxt] == dist[left] + 1 and nxt not in visited:
                    iters[left] = pos
                    chosen[left] = right
                    stack.append(nxt)
                    iters[nxt] = 0
                    visited.add(nxt)
                    advanced = True
                    break
            if advanced:
                continue
            iters[left] = pos
            dist[left] = -1
            stack.pop()
        return False

    def run(self):
        n_left = self.n_left
        match_left = self.match_left
        match_right = self.match_right
        adj = self.adj
        while True:
            dist = [-1] * n_left
            queue = deque()
            for left in range(n_left):
                if match_left[left] == -1:
                    dist[left] = 0
                    queue.append(left)
            found = False
            while queue:
                left = queue.popleft()
                step = dist[left] + 1
                for right in adj[left]:
                    nxt = match_right[right]
                    if nxt == -1:
                        found = True
                    elif dist[nxt] == -1:
                        dist[nxt] = step
                        queue.append(nxt)
            if not found:
                break
            for left in range(n_left):
                if match_left[left] == -1 and dist[left] == 0:
                    self._augment(left, dist)
        return self

    def size(self):
        return sum(1 for left in range(self.n_left) if self.match_left[left] != -1)

def _alternating_indices(matcher):
    """Return ``(from_exposed_left, to_exposed_right, component)``.

    ``from_exposed_left`` is the set of left indices reachable from an exposed
    left vertex; ``to_exposed_right`` is the set of right indices that can reach
    an exposed right vertex; ``component`` maps every node (left indices
    ``0..n_left-1`` and right indices offset by ``n_left``) to its SCC id in the
    alternating digraph.
    """
    n_left = matcher.n_left
    n_right = matcher.n_right
    match_left = matcher.match_left
    match_right = matcher.match_right

    # Forward digraph: unmatched L->R, matched R->L.
    digraph = [[] for _ in range(n_left + n_right)]
    reverse = [[] for _ in range(n_left + n_right)]
    for left in range(n_left):
        matched = match_left[left]
        for right in matcher.adj[left]:
            if right == matched:
                a, b = n_left + right, left
            else:
                a, b = left, n_left + right
            digraph[a].append(b)
            reverse[b].append(a)

    def reachable(starts, graph):
        seen = set(starts)
        stack = list(starts)
        while stack:
            node = stack.pop()
            for nxt in graph[node]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    exposed_left = [l for l in range(n_left) if match_left[l] == -1]
    exposed_right = [n_left + r for r in range(n_right) if match_right[r] == -1]
    from_left = reachable(exposed_left, digraph)
    to_right = reachable(exposed_right, reverse)

    component = _tarjan(digraph)
    return from_left, to_right, component


def _tarjan(digraph):
    """Iterative Tarjan SCC; returns node->component id."""
    index = {}
    low = {}
    on_stack = set()
    stack = []
    component = {}
    counter = 0
    for start in range(len(digraph)):
        if start in index:
            continue
        work = [(start, 0)]
        while work:
            node, pos = work[-1]
            if pos == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            successors = digraph[node]
            if pos < len(successors):
                work[-1] = (node, pos + 1)
                nxt = successors[pos]
                if nxt not in index:
                    work.append((nxt, 0))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            else:
                work.pop()
                if low[node] == index[node]:
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component[member] = index[node]
                        if member == node:
                            break
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
    return component


# ---------------------------------------------------------------------------
# report construction
# ---------------------------------------------------------------------------


def _classify_candidates(candidates, known_bags, scope):
    """Split one read's raw candidates into in-scope / unknown / out-of-scope."""
    classified = []
    for bag_id in sorted(candidates):
        if bag_id in scope:
            kind = CANDIDATE_IN_SCOPE
        elif bag_id in known_bags:
            kind = CANDIDATE_OUT_OF_SCOPE
        else:
            kind = CANDIDATE_UNKNOWN
        classified.append({"bag_id": bag_id, "class": kind})
    return classified


def _read_window(reads, capture_from, capture_until):
    """Return the in-window reads keyed by read_id."""
    in_window = {}
    for read in reads:
        if capture_from <= read["instant"] < capture_until:
            in_window[read["read_id"]] = read
    return in_window


def build_report(archive_path, as_of, batch_path, decisions_path=None):
    """Compute the read-audit report without writing anything."""
    batch = _parse_batch(archive._load_json(batch_path))
    decisions = (
        _parse_decisions(archive._load_json(decisions_path))
        if decisions_path is not None
        else {"confirm": [], "discard": []}
    )

    with archive._locked(archive_path):
        events = archive._load_events(archive_path)
        _cutoff(events, as_of)
        bags, _scans, flights = archive._project(events, as_of)

    known_bags = {bag["id"] for bag in bags}
    scope = _scope_bags(
        bags, flights, batch["station"],
        batch["departure_from"], batch["departure_until"],
    )
    in_window = _read_window(
        batch["reads"], batch["capture_from"], batch["capture_until"]
    )

    # Checkable (in-scope) candidates per in-window read.
    in_scope_candidates = {
        read_id: [item["bag_id"] for item in
                  _classify_candidates(read["candidates"], known_bags, scope)
                  if item["class"] == CANDIDATE_IN_SCOPE]
        for read_id, read in in_window.items()
    }
    resolved = _validate_decisions(decisions, in_window, in_scope_candidates)

    confirmed_read_bag = {
        read_id: entry["bag_id"]
        for read_id, entry in resolved.items()
        if entry["action"] == "confirm"
    }
    confirmed_bags = {bag_id: read_id for read_id, bag_id in confirmed_read_bag.items()}
    discarded = {
        read_id for read_id, entry in resolved.items() if entry["action"] == "discard"
    }

    # Left vertices: every in-window read that was not discarded.  A confirmed
    # read keeps only its fixed edge and every other read loses any edge into a
    # confirmed bag, because a confirmed bag can never take another read.
    left_reads = sorted(read_id for read_id in in_window if read_id not in discarded)
    bag_ids = sorted(scope)
    read_index = {read_id: i for i, read_id in enumerate(left_reads)}
    bag_index = {bag_id: i for i, bag_id in enumerate(bag_ids)}

    adjacency = []
    for read_id in left_reads:
        if read_id in confirmed_read_bag:
            allowed = [confirmed_read_bag[read_id]]
        else:
            allowed = [
                bag for bag in in_scope_candidates[read_id]
                if bag not in confirmed_bags
            ]
        adjacency.append(sorted({bag_index[bag] for bag in allowed}))

    matcher = _Matcher(len(left_reads), len(bag_ids), adjacency)
    for read_id in sorted(confirmed_read_bag):
        matcher.force(read_index[read_id], bag_index[confirmed_read_bag[read_id]])
    matcher.run()

    coverage = matcher.size()
    min_missing = len(bag_ids) - coverage
    from_left, to_right, component = _alternating_indices(matcher)
    n_left = matcher.n_left

    def edge_allowed(left, right):
        if matcher.match_left[left] == right:
            return True
        right_node = n_left + right
        return (
            left in from_left
            or right_node in to_right
            or component[left] == component[right_node]
        )

    bag_reads = [[] for _ in range(matcher.n_right)]
    read_bags = [[] for _ in range(matcher.n_left)]
    for left in range(matcher.n_left):
        for right in adjacency[left]:
            if edge_allowed(left, right):
                bag_reads[right].append(left_reads[left])
                read_bags[left].append(bag_ids[right])

    # ---- bags -------------------------------------------------------------
    bag_rows = []
    counts = {EVIDENCE_CERTAIN: 0, EVIDENCE_POSSIBLE: 0, EVIDENCE_MISSING: 0}
    for right, bag_id in enumerate(bag_ids):
        reads_for_bag = sorted(bag_reads[right])
        if not reads_for_bag:
            status = EVIDENCE_MISSING
        elif (n_left + right) in to_right:
            status = EVIDENCE_POSSIBLE
        else:
            status = EVIDENCE_CERTAIN
        counts[status] += 1
        info = scope[bag_id]
        bag_rows.append(
            {
                "bag_id": bag_id,
                "next_flight": info["flight"],
                "source": info["source"],
                "evidence_status": status,
                "evidence_read_ids": reads_for_bag,
                "can_be_missing": status != EVIDENCE_CERTAIN,
            }
        )

    # ---- reads ------------------------------------------------------------
    read_rows = []
    review_ids = []
    read_counts = {
        "in_window": 0,
        "outside_window": 0,
        STATUS_CONFIRMED: 0,
        STATUS_DISCARDED: 0,
        STATUS_ALWAYS_USED: 0,
        STATUS_OPTIONAL: 0,
        STATUS_UNUSED: 0,
    }
    for read in batch["reads"]:
        read_id = read["read_id"]
        classified = _classify_candidates(read["candidates"], known_bags, scope)
        unknown = [item["bag_id"] for item in classified
                   if item["class"] == CANDIDATE_UNKNOWN]
        out_of_scope = [item["bag_id"] for item in classified
                        if item["class"] == CANDIDATE_OUT_OF_SCOPE]
        in_scope = [item["bag_id"] for item in classified
                    if item["class"] == CANDIDATE_IN_SCOPE]
        decision = resolved.get(read_id)
        row = {
            "read_id": read_id,
            "captured_at": read["captured_at"],
            "in_capture_window": read_id in in_window,
            "candidates": classified,
            "unknown_candidate_bag_ids": unknown,
            "out_of_scope_candidate_bag_ids": out_of_scope,
            "in_scope_candidate_bag_ids": in_scope,
            "allowed_bag_ids": [],
            "may_be_idle": True,
            "always_used": False,
            "proof_ref": None,
            "needs_review": False,
        }
        if read_id not in in_window:
            row["status"] = STATUS_OUTSIDE
            read_counts["outside_window"] += 1
            read_rows.append(row)
            continue

        read_counts["in_window"] += 1
        if decision is not None and decision["action"] == "confirm":
            row["status"] = STATUS_CONFIRMED
            row["allowed_bag_ids"] = [decision["bag_id"]]
            row["may_be_idle"] = False
            row["always_used"] = True
            row["proof_ref"] = decision["proof_ref"]
            read_counts[STATUS_CONFIRMED] += 1
            read_rows.append(row)
            continue
        if decision is not None and decision["action"] == "discard":
            row["status"] = STATUS_DISCARDED
            row["may_be_idle"] = True
            row["always_used"] = False
            row["proof_ref"] = decision["proof_ref"]
            read_counts[STATUS_DISCARDED] += 1
            read_rows.append(row)
            continue

        left = read_index[read_id]
        bags_for_read = sorted(read_bags[left])
        row["allowed_bag_ids"] = bags_for_read
        if not bags_for_read:
            # No usable candidate at all (unknown / out of scope / confirmed
            # away): naturally unattributable, never part of any scheme.
            status = STATUS_UNUSED
            idle = True
        elif left in from_left:
            status = STATUS_OPTIONAL
            idle = True
        else:
            status = STATUS_ALWAYS_USED
            idle = False
        row["status"] = status
        row["may_be_idle"] = idle
        row["always_used"] = not idle
        read_counts[status] += 1
        # A human is still needed unless the read is spent on one and only one
        # bag in every maximum scheme: a read that could serve several bags, or
        # that could idle, leaves a genuine ambiguity to resolve from the image.
        row["needs_review"] = not (
            status == STATUS_ALWAYS_USED and len(bags_for_read) == 1
        )
        if row["needs_review"]:
            review_ids.append(read_id)
        read_rows.append(row)

    read_rows.sort(key=lambda item: item["read_id"])

    # ---- example pairing ---------------------------------------------------
    example = []
    for left, read_id in enumerate(left_reads):
        right = matcher.match_left[left]
        if right == -1:
            continue
        example.append(
            {
                "read_id": read_id,
                "bag_id": bag_ids[right],
                "basis": PAIR_MANUAL if read_id in confirmed_read_bag else PAIR_RECONCILED,
            }
        )
    example.sort(key=lambda item: item["read_id"])

    # ---- decisions echo ----------------------------------------------------
    confirm_echo = [
        {
            "read_id": entry["read_id"],
            "bag_id": entry["bag_id"],
            "proof_ref": entry["proof_ref"],
        }
        for entry in decisions["confirm"]
    ]
    confirm_echo.sort(key=lambda item: (item["read_id"], item["bag_id"]))
    discard_echo = [
        {"read_id": entry["read_id"], "proof_ref": entry["proof_ref"]}
        for entry in decisions["discard"]
    ]
    discard_echo.sort(key=lambda item: item["read_id"])

    total_candidate_refs = sum(len(read["candidates"]) for read in batch["reads"])
    in_scope_refs = sum(len(row["in_scope_candidate_bag_ids"]) for row in read_rows)
    unknown_refs = sum(len(row["unknown_candidate_bag_ids"]) for row in read_rows)
    out_scope_refs = sum(
        len(row["out_of_scope_candidate_bag_ids"]) for row in read_rows
    )

    return {
        "format": REPORT_FORMAT,
        "knowledge_seq": as_of,
        "station": batch["station"],
        "windows": {
            "departure": {
                "from": batch["departure_from_text"],
                "until": batch["departure_until_text"],
            },
            "capture": {
                "from": batch["capture_from_text"],
                "until": batch["capture_until_text"],
            },
        },
        "decisions": {"confirm": confirm_echo, "discard": discard_echo},
        "bags": bag_rows,
        "reads": read_rows,
        "example_pairing": example,
        "summary": {
            "scoped_bag_count": len(bag_ids),
            "max_coverage": coverage,
            "min_missing": min_missing,
            "bag_evidence": counts,
            "read_status": read_counts,
            "candidate_refs": {
                "total": total_candidate_refs,
                "in_scope": in_scope_refs,
                "unknown": unknown_refs,
                "out_of_scope": out_scope_refs,
            },
            "manual_review_read_ids": sorted(set(review_ids)),
        },
    }


def write_report(archive_path, as_of, batch_path, output_path, decisions_path=None):
    """Build the report fully, then create ``output_path`` exclusively."""
def write_report(archive_path, as_of, batch_path, output_path, decisions_path=None):
    """Build the report fully, then publish it without ever overwriting.

    The JSON is rendered and fsynced to a sibling temporary file first and then
    hard-linked into place, so an interrupted or failing write never leaves a
    partial report and never replaces an existing one.
    """
    report = build_report(archive_path, as_of, batch_path, decisions_path)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"output already exists: {target}")
    temp = target.parent / f".{target.name}.tmp-{os.getpid()}-{time.time_ns()}"
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, target)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass
    return report

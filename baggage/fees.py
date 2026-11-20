"""Local monthly evidence-fee settlement ledger for a baggage archive.

A station pays a fixed *evidence service fee* for each transfer service whose
documentary record satisfies the contract.  The amount records evidence only:
it is not proof that a bag was physically transferred and it never affects
screening, loading, or handoff operations.

The ledger is one JSON document, ``<archive>/fee-ledger.json``, beside the
untouched ``events.jsonl`` archive.  It holds the fixed rate table, every
first-close monthly statement, every correction entry, and the last cutoff a
write command successfully processed.  It is replaced atomically, so a failed
command can never leave a partial cross-month correction, a truncated
statement, or an advanced cutoff.

Accounting model
----------------

* A *service* is the pair ``(bag_id, next_flight_id)`` of a bag that has a
  next flight.  Its prior leg is the bag's fixed ``flight_id``.
* The attribution station is the next flight's departure station; the
  attribution month is the UTC calendar month of the next flight's scheduled
  departure instant, using the flight/scan versions effective at the requested
  ``knowledge_seq``.
* A service is billable when the two legs differ, the prior leg arrives at
  the next leg's departure station no later than that departure, the station
  has a rate, the bag's minimum connection is a non-negative integer, the
  evidence window is non-empty, and at least one active scan of the bag at
  the attribution station lies inside the inclusive window.
* One *effective charge* per service may exist at a time.  A correction first
  reverses that charge when its attribution changed or disappeared, then adds
  a new positive charge only when the new attribution month is already closed.
  Reversed lines stay in the ledger; a service that moves back receives a new
  positive line and never revives the reversed one.

Python API (standard library only)::

    init_ledger(archive_path, rates_path)
    preview_month(archive_path, month, as_of)
    close_month(archive_path, month, as_of)
    correct_ledger(archive_path, as_of)
    show_month(archive_path, month)
"""

import json
import os
import re
import time
from datetime import timedelta, timezone
from pathlib import Path

from . import archive
from .core import _time

LEDGER_FILE = "fee-ledger.json"
LEDGER_VERSION = 1
_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_LINE_RE = re.compile(r"^L(\d+)$")

# Deterministic order for the reasons a service is not billable.
_REASON_ORDER = (
    "same_leg",
    "prior_arrival_station_mismatch",
    "prior_arrival_after_next_departure",
    "no_rate_for_station",
    "invalid_minimum_connection_minutes",
    "empty_evidence_window",
    "no_station_evidence",
)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _months(value):
    if not isinstance(value, str) or not _MONTH_RE.match(value):
        raise ValueError(f"invalid month {value!r}; expected YYYY-MM")
    return value


def _require_int(value, label, minimum=None):
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return value


def _reasons(items):
    return sorted(set(items), key=_REASON_ORDER.index)


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path, payload):
    """Replace ``path`` atomically so readers see the old or the new ledger."""
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


def _line_number(line_id):
    match = _LINE_RE.match(line_id) if isinstance(line_id, str) else None
    if match is None:
        raise ValueError(f"corrupt fee ledger: invalid line id {line_id!r}")
    return int(match.group(1))


# ---------------------------------------------------------------------------
# rate table and ledger storage
# ---------------------------------------------------------------------------


def _validate_rates(rates, label="rate table"):
    if not isinstance(rates, dict):
        raise ValueError(f"{label} must be a JSON object")
    for station, amount in rates.items():
        if not isinstance(station, str) or not station:
            raise ValueError(f"{label} keys must be non-empty station codes")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise ValueError(
                f"{label} value for station {station!r} must be a positive integer "
                "number of cents"
            )
    return dict(rates)


def _load_rates(path):
    try:
        rates = archive._load_json(path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid rate table JSON: {exc}") from exc
    return _validate_rates(rates)


def _ledger_path(archive_path):
    return Path(archive_path) / LEDGER_FILE


def _empty_ledger(rates):
    return {
        "version": LEDGER_VERSION,
        "rates": rates,
        "last_processed_seq": None,
        "statements": {},
        "corrections": [],
        "line_counter": 0,
        "correction_counter": 0,
    }


def _validate_statement(month, statement):
    if not isinstance(statement, dict):
        raise ValueError("corrupt fee ledger: statement must be an object")
    if statement.get("month") != month:
        raise ValueError("corrupt fee ledger: statement month does not match its key")
    _require_int(statement.get("knowledge_seq"), "statement knowledge_seq", 1)
    lines = statement.get("lines")
    if not isinstance(lines, list):
        raise ValueError("corrupt fee ledger: statement lines must be a list")
    total = 0
    for line in lines:
        _validate_line(line)
        if line["kind"] != "charge":
            raise ValueError("corrupt fee ledger: a first-close statement may only charge")
        if line["month"] != month:
            raise ValueError("corrupt fee ledger: statement line month does not match")
        if line.get("knowledge_seq") != statement["knowledge_seq"]:
            raise ValueError(
                "corrupt fee ledger: statement line cutoff does not match its statement"
            )
        total += line["amount_cents"]
    if statement.get("total_amount_cents") != total:
        raise ValueError("corrupt fee ledger: statement total does not match its lines")


def _validate_line(line):
    if not isinstance(line, dict):
        raise ValueError("corrupt fee ledger: line must be an object")
    _line_number(line.get("line_id"))
    for field in ("bag_id", "next_flight_id", "station", "month"):
        if not isinstance(line.get(field), str) or not line[field]:
            raise ValueError(f"corrupt fee ledger: line missing {field!r}")
    _months(line["month"])
    _require_int(line.get("amount_cents"), "line amount_cents")
    _require_int(line.get("knowledge_seq"), "line knowledge_seq", 1)
    kind = line.get("kind")
    if kind == "charge":
        if line["amount_cents"] <= 0:
            raise ValueError("corrupt fee ledger: a charge must be positive")
        if line.get("reverses_line_id") is not None:
            raise ValueError("corrupt fee ledger: a charge cannot reverse a line")
        if not isinstance(line.get("basis"), dict):
            raise ValueError("corrupt fee ledger: a charge requires its basis")
    elif kind == "reversal":
        if line["amount_cents"] >= 0:
            raise ValueError("corrupt fee ledger: a reversal must be negative")
        _line_number(line.get("reverses_line_id"))
        _require_int(line.get("reverses_amount_cents"), "reversal amount", 1)
        if line["reverses_amount_cents"] != -line["amount_cents"]:
            raise ValueError("corrupt fee ledger: reversal amount does not mirror its charge")
        if not isinstance(line.get("reason"), str) or not line["reason"]:
            raise ValueError("corrupt fee ledger: a reversal requires a reason")
        if not isinstance(line.get("current_basis"), dict):
            raise ValueError("corrupt fee ledger: a reversal requires its current basis")
    else:
        raise ValueError("corrupt fee ledger: unknown line kind")


def _validate_correction(correction):
    if not isinstance(correction, dict):
        raise ValueError("corrupt fee ledger: correction must be an object")
    correction_id = correction.get("correction_id")
    if not isinstance(correction_id, str) or not correction_id:
        raise ValueError("corrupt fee ledger: correction requires an id")
    _require_int(correction.get("knowledge_seq"), "correction knowledge_seq", 1)
    if not isinstance(correction.get("lines"), list):
        raise ValueError("corrupt fee ledger: correction lines must be a list")
    for line in correction["lines"]:
        _validate_line(line)
        if line.get("knowledge_seq") != correction["knowledge_seq"]:
            raise ValueError(
                "corrupt fee ledger: correction line cutoff does not match its correction"
            )


def _validate_ledger(state):
    if not isinstance(state, dict) or state.get("version") != LEDGER_VERSION:
        raise ValueError("corrupt fee ledger: unsupported layout")
    _validate_rates(state.get("rates"), "ledger rate table")
    last = state.get("last_processed_seq")
    if last is not None:
        _require_int(last, "last_processed_seq", 1)
    statements = state.get("statements")
    if not isinstance(statements, dict):
        raise ValueError("corrupt fee ledger: statements must be an object")
    for month, statement in statements.items():
        _months(month)
        _validate_statement(month, statement)
    corrections = state.get("corrections")
    if not isinstance(corrections, list):
        raise ValueError("corrupt fee ledger: corrections must be a list")
    for correction in corrections:
        _validate_correction(correction)
    _require_int(state.get("line_counter"), "line_counter", 0)
    _require_int(state.get("correction_counter"), "correction_counter", 0)
    # Also checks that charges and reversals form a consistent chain.
    _effective_charges(state)
    return state


def _load_ledger(archive_path):
    path = _ledger_path(archive_path)
    if not path.is_file():
        raise ValueError(
            f"fee ledger does not exist for archive {archive_path}; run fee-init first"
        )
    try:
        state = archive._load_json(path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"corrupt fee ledger: {exc}") from exc
    return _validate_ledger(state)


def _save_ledger(archive_path, state):
    _atomic_write(_ledger_path(archive_path), state)


def _effective_charges(ledger):
    """Return ``(active, ever_charged, ordered_lines)`` for the whole ledger.

    Lines are ordered by their monotonically assigned line number, which is the
    true application order even though statements live in a month-keyed mapping
    and corrections live in a list.
    """
    lines = []
    for statement in ledger["statements"].values():
        lines.extend(statement["lines"])
    for correction in ledger["corrections"]:
        lines.extend(correction["lines"])
    lines.sort(key=lambda line: _line_number(line["line_id"]))
    seen = set()
    active = {}
    ever_charged = set()
    for line in lines:
        line_id = line["line_id"]
        if line_id in seen:
            raise ValueError(f"corrupt fee ledger: duplicate line id {line_id}")
        seen.add(line_id)
        if _line_number(line_id) > ledger["line_counter"]:
            raise ValueError("corrupt fee ledger: line id exceeds the line counter")
        key = (line["bag_id"], line["next_flight_id"])
        if line["kind"] == "charge":
            if key in active:
                raise ValueError(
                    "corrupt fee ledger: two effective charges for one service"
                )
            active[key] = line
            ever_charged.add(key)
        else:
            current = active.get(key)
            if current is None or current["line_id"] != line["reverses_line_id"]:
                raise ValueError(
                    "corrupt fee ledger: reversal does not match the effective charge"
                )
            if (
                line["station"] != current["station"]
                or line["month"] != current["month"]
            ):
                raise ValueError(
                    "corrupt fee ledger: reversal does not sit in the charge's "
                    "station and month"
                )
            del active[key]
    return active, ever_charged, lines


# ---------------------------------------------------------------------------
# archive projection and billability
# ---------------------------------------------------------------------------


def _cutoff(events, as_of):
    max_seq = events[-1]["knowledge_seq"]
    if not isinstance(as_of, int) or isinstance(as_of, bool) or as_of < 1 or as_of > max_seq:
        raise ValueError(f"unknown or unaccepted knowledge_seq: {as_of}")
    return as_of


def _check_cutoff_order(ledger, as_of):
    last = ledger.get("last_processed_seq")
    if last is not None and as_of < last:
        raise ValueError(
            f"knowledge_seq {as_of} is older than the last processed cutoff {last}"
        )


def _utc_month(value):
    instant = _time(value).astimezone(timezone.utc)
    return f"{instant.year:04d}-{instant.month:02d}"


def _flight_snapshot(flight_id, entry):
    record = entry["record"]
    return {
        "flight_id": flight_id,
        "origin": record["origin"],
        "destination": record["destination"],
        "departure": record["departure"],
        "arrival": record["arrival"],
        "knowledge_seq": entry["knowledge_seq"],
        "batch_id": entry["batch_id"],
    }


def _scan_snapshot(scan_id, entry):
    record = entry["record"]
    return {
        "scan_id": scan_id,
        "bag_id": record["bag_id"],
        "station": record["station"],
        "event_time": record["event_time"],
        "knowledge_seq": entry["knowledge_seq"],
        "batch_id": entry["batch_id"],
    }


def _valid_minimum(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _evaluate_service(bag, flights, scan_index, rates):
    bag_id = bag["id"]
    prior_id = bag["flight_id"]
    next_id = bag["next_flight_id"]
    prior_entry = flights[prior_id]
    next_entry = flights[next_id]
    prior = prior_entry["record"]
    nxt = next_entry["record"]
    station = nxt["origin"]

    reasons = []
    if prior_id == next_id:
        reasons.append("same_leg")
    if prior["destination"] != nxt["origin"]:
        reasons.append("prior_arrival_station_mismatch")

    prior_arrival = _time(prior["arrival"])
    next_departure = _time(nxt["departure"])
    if prior_arrival > next_departure:
        reasons.append("prior_arrival_after_next_departure")

    rate = rates.get(station)
    if rate is None:
        reasons.append("no_rate_for_station")

    minimum = bag.get("minimum_connection_minutes")
    window_start = None
    window_end = None
    adopted_scan = None
    qualifying = []
    if _valid_minimum(minimum):
        end_instant = next_departure - timedelta(minutes=minimum)
        window_start = prior_arrival.astimezone(timezone.utc).isoformat()
        window_end = end_instant.astimezone(timezone.utc).isoformat()
        if prior_arrival > end_instant:
            reasons.append("empty_evidence_window")
        else:
            for event_instant, scan_id, entry in scan_index.get((bag_id, station), ()):
                if prior_arrival <= event_instant <= end_instant:
                    qualifying.append(scan_id)
                    if adopted_scan is None:
                        adopted_scan = _scan_snapshot(scan_id, entry)
            if not qualifying:
                reasons.append("no_station_evidence")
    else:
        reasons.append("invalid_minimum_connection_minutes")

    reasons = _reasons(reasons)
    billable = not reasons
    return {
        "bag_id": bag_id,
        "next_flight_id": next_id,
        "prior_flight_id": prior_id,
        "station": station,
        "month": _utc_month(nxt["departure"]),
        "billable": billable,
        "reasons": reasons,
        "amount_cents": rate if billable else None,
        "rate_cents": rate,
        "minimum_connection_minutes": minimum,
        "window_start": window_start,
        "window_end": window_end,
        "adopted_scan": adopted_scan,
        "qualifying_scan_ids": qualifying,
        "prior_flight": _flight_snapshot(prior_id, prior_entry),
        "next_flight": _flight_snapshot(next_id, next_entry),
    }


def _evaluations(events, as_of, rates):
    bags, scans, flights = archive._project(events, as_of)
    scan_index = {}
    for scan_id, entry in scans.items():
        record = entry["record"]
        scan_index.setdefault((record["bag_id"], record["station"]), []).append(
            (_time(record["event_time"]), scan_id, entry)
        )
    for items in scan_index.values():
        items.sort(key=lambda item: (item[0], item[1]))

    result = {}
    for bag in sorted(bags, key=lambda item: item["id"]):
        next_flight_id = bag.get("next_flight_id")
        if next_flight_id is None:
            continue
        evaluation = _evaluate_service(bag, flights, scan_index, rates)
        result[(bag["id"], next_flight_id)] = evaluation
    return result


def _basis(evaluation):
    return {
        "prior_flight": evaluation["prior_flight"],
        "next_flight": evaluation["next_flight"],
        "evidence": {
            "window_start": evaluation["window_start"],
            "window_end": evaluation["window_end"],
            "minimum_connection_minutes": evaluation["minimum_connection_minutes"],
            "adopted_scan": evaluation["adopted_scan"],
            "qualifying_scan_ids": list(evaluation["qualifying_scan_ids"]),
        },
        "rate_cents": evaluation["rate_cents"],
    }


def _targets(evaluations):
    return {key: value for key, value in evaluations.items() if value["billable"]}


def _summarise(candidates):
    by_station = {}
    total = 0
    count = 0
    for candidate in candidates:
        if not candidate["billable"]:
            continue
        count += 1
        total += candidate["amount_cents"]
        entry = by_station.setdefault(
            candidate["station"],
            {"station": candidate["station"], "billable_count": 0, "amount_cents": 0},
        )
        entry["billable_count"] += 1
        entry["amount_cents"] += candidate["amount_cents"]
    return {
        "candidate_count": len(candidates),
        "billable_count": count,
        "non_billable_count": len(candidates) - count,
        "amount_cents": total,
        "by_station": [by_station[station] for station in sorted(by_station)],
    }


# ---------------------------------------------------------------------------
# correction planning
# ---------------------------------------------------------------------------


def _charge_line(ledger, key, evaluation, reason, detail):
    ledger["line_counter"] += 1
    return {
        "line_id": f"L{ledger['line_counter']:08d}",
        "kind": "charge",
        "bag_id": key[0],
        "next_flight_id": key[1],
        "station": evaluation["station"],
        "month": evaluation["month"],
        "amount_cents": evaluation["amount_cents"],
        "reverses_line_id": None,
        "reason": reason,
        "detail": detail,
        "basis": _basis(evaluation),
    }


def _reversal_line(ledger, key, effective, reason, detail, evaluation):
    ledger["line_counter"] += 1
    return {
        "line_id": f"L{ledger['line_counter']:08d}",
        "kind": "reversal",
        "bag_id": key[0],
        "next_flight_id": key[1],
        "station": effective["station"],
        "month": effective["month"],
        "amount_cents": -effective["amount_cents"],
        "reverses_line_id": effective["line_id"],
        "reverses_amount_cents": effective["amount_cents"],
        "reason": reason,
        "detail": detail,
        "current_basis": _basis(evaluation),
    }


def _stamp_cutoff(lines, as_of):
    for line in lines:
        line["knowledge_seq"] = as_of
    return lines


def _correction_lines(ledger, evaluations, targets, closing_month=None):
    """Build the correction lines for the current cutoff (no writes).

    Only months already in ``ledger["statements"]`` receive correction charges;
    ``closing_month`` (the month a first close is adding in the same operation)
    is not yet closed, so a reattribution into it only reverses here and the
    statement supplies the single positive line. ``closing_month`` affects only
    the audited ``new_charge_recorded`` detail, since the same operation does
    record that charge.
    """
    active, ever_charged, _ = _effective_charges(ledger)
    closed = set(ledger["statements"])
    recorded_now = closed | ({closing_month} if closing_month else set())
    lines = []
    for key in sorted(set(active) | set(targets)):
        effective = active.get(key)
        target = targets.get(key)
        # Bags are immutable in the archive, so every effective charge's
        # service is always present in this cutoff's evaluation.
        evaluation = evaluations[key]

        if effective is not None and target is not None:
            same = (
                effective["station"] == target["station"]
                and effective["month"] == target["month"]
                and effective["amount_cents"] == target["amount_cents"]
            )
            if same:
                # Evidence versions may differ, but the financial position is
                # unchanged: no ledger row is created.
                continue
            if (
                effective["station"] == target["station"]
                and effective["month"] == target["month"]
            ):
                reason = "amount_changed"
                detail = {"new_amount_cents": target["amount_cents"]}
            else:
                reason = "reattributed"
                detail = {
                    "new_station": target["station"],
                    "new_month": target["month"],
                    "new_amount_cents": target["amount_cents"],
                    "new_charge_recorded": target["month"] in recorded_now,
                }
            lines.append(_reversal_line(ledger, key, effective, reason, detail, evaluation))
            if target["month"] in closed:
                lines.append(
                    _charge_line(
                        ledger,
                        key,
                        target,
                        "reattribution",
                        {
                            "supersedes_line_id": effective["line_id"],
                            "previous_station": effective["station"],
                            "previous_month": effective["month"],
                        },
                    )
                )
        elif effective is not None:
            lines.append(
                _reversal_line(
                    ledger,
                    key,
                    effective,
                    "not_billable",
                    {"unmet_reasons": list(evaluation["reasons"])},
                    evaluation,
                )
            )
        elif target is not None and target["month"] in closed:
            if key in ever_charged:
                reason = "returned"
                detail = {"returned_after_reversal": True}
            else:
                reason = "supplemental"
                detail = {"late_evidence": True}
            lines.append(_charge_line(ledger, key, target, reason, detail))
    return lines


def _append_correction(ledger, as_of, lines):
    if not lines:
        return None
    ledger["correction_counter"] += 1
    _stamp_cutoff(lines, as_of)
    correction = {
        "correction_id": f"X{ledger['correction_counter']}",
        "knowledge_seq": as_of,
        "lines": lines,
    }
    ledger["corrections"].append(correction)
    return correction


def _correction_summary(correction):
    if correction is None:
        return None
    reversed_amount = sum(
        line["amount_cents"] for line in correction["lines"] if line["kind"] == "reversal"
    )
    charged_amount = sum(
        line["amount_cents"] for line in correction["lines"] if line["kind"] == "charge"
    )
    return {
        "correction_id": correction["correction_id"],
        "knowledge_seq": correction["knowledge_seq"],
        "line_count": len(correction["lines"]),
        "line_ids": [line["line_id"] for line in correction["lines"]],
        "reversed_amount_cents": reversed_amount,
        "charged_amount_cents": charged_amount,
        "net_amount_cents": reversed_amount + charged_amount,
    }


# ---------------------------------------------------------------------------
# 1. initialise
# ---------------------------------------------------------------------------


def init_ledger(archive_path, rates_path):
    """Register the fixed rate table and an empty monthly settlement ledger."""
    rates = _load_rates(rates_path)
    with archive._locked(archive_path):
        archive._load_events(archive_path)
        path = _ledger_path(archive_path)
        if path.exists():
            raise ValueError(f"fee ledger already exists for archive {archive_path}")
        _save_ledger(archive_path, _empty_ledger(rates))
    return {
        "archive": str(archive_path),
        "ledger": str(path),
        "stations": sorted(rates),
        "amount_unit": "cents",
    }


# ---------------------------------------------------------------------------
# 2. preview
# ---------------------------------------------------------------------------


def preview_month(archive_path, month, as_of):
    """Read-only recomputation of one month at one archive cutoff."""
    month = _months(month)
    with archive._locked(archive_path):
        events = archive._load_events(archive_path)
        _cutoff(events, as_of)
        ledger = _load_ledger(archive_path)
        evaluations = _evaluations(events, as_of, ledger["rates"])
        candidates = [
            evaluation for evaluation in evaluations.values() if evaluation["month"] == month
        ]
        candidates.sort(key=lambda item: (item["bag_id"], item["next_flight_id"]))
        return {
            "month": month,
            "knowledge_seq": as_of,
            "recomputation": True,
            "accounting_effect": "none",
            "month_closed": month in ledger["statements"],
            "last_processed_seq": ledger["last_processed_seq"],
            "candidates": candidates,
            "summary": _summarise(candidates),
        }


# ---------------------------------------------------------------------------
# 3. first close
# ---------------------------------------------------------------------------


def close_month(archive_path, month, as_of):
    """First-close one month, applying all due corrections to older months."""
    month = _months(month)
    with archive._locked(archive_path):
        events = archive._load_events(archive_path)
        _cutoff(events, as_of)
        ledger = _load_ledger(archive_path)
        if month in ledger["statements"]:
            raise ValueError(f"month {month} is already closed and cannot be reissued")
        _check_cutoff_order(ledger, as_of)

        evaluations = _evaluations(events, as_of, ledger["rates"])
        targets = _targets(evaluations)

        correction = _append_correction(
            ledger,
            as_of,
            _correction_lines(ledger, evaluations, targets, closing_month=month),
        )

        statement_lines = []
        # A month can only be first-closed once, so no effective charge can
        # already sit in it; every current target in this month is new.
        for key in sorted(targets):
            target = targets[key]
            if target["month"] != month:
                continue
            statement_lines.append(
                _charge_line(ledger, key, target, "first_close", {"closed_month": month})
            )
        total = sum(line["amount_cents"] for line in statement_lines)
        _stamp_cutoff(statement_lines, as_of)
        statement = {
            "month": month,
            "knowledge_seq": as_of,
            "lines": statement_lines,
            "total_amount_cents": total,
        }
        ledger["statements"][month] = statement
        ledger["last_processed_seq"] = as_of
        _save_ledger(archive_path, ledger)

        return {
            "month": month,
            "knowledge_seq": as_of,
            "closed": True,
            "statement": {
                "month": month,
                "knowledge_seq": as_of,
                "line_count": len(statement_lines),
                "line_ids": [line["line_id"] for line in statement_lines],
                "total_amount_cents": total,
            },
            "corrections": _correction_summary(correction),
            "last_processed_seq": as_of,
        }


# ---------------------------------------------------------------------------
# 4. recheck closed months
# ---------------------------------------------------------------------------


def correct_ledger(archive_path, as_of):
    """Recheck every closed month and append any due correction lines."""
    with archive._locked(archive_path):
        events = archive._load_events(archive_path)
        _cutoff(events, as_of)
        ledger = _load_ledger(archive_path)
        if not ledger["statements"]:
            raise ValueError("fee-correct requires at least one closed month")
        _check_cutoff_order(ledger, as_of)

        evaluations = _evaluations(events, as_of, ledger["rates"])
        targets = _targets(evaluations)
        correction = _append_correction(
            ledger, as_of, _correction_lines(ledger, evaluations, targets)
        )
        ledger["last_processed_seq"] = as_of
        _save_ledger(archive_path, ledger)

        summary = _correction_summary(correction)
        return {
            "knowledge_seq": as_of,
            "closed_months": sorted(ledger["statements"]),
            "corrections": summary,
            "lines_added": 0 if summary is None else summary["line_count"],
            "reversed_amount_cents": 0 if summary is None else summary["reversed_amount_cents"],
            "charged_amount_cents": 0 if summary is None else summary["charged_amount_cents"],
            "net_amount_cents": 0 if summary is None else summary["net_amount_cents"],
            "last_processed_seq": as_of,
        }


# ---------------------------------------------------------------------------
# 5. show
# ---------------------------------------------------------------------------


def _line_view(line, lookup):
    view = json.loads(json.dumps(line))
    if line["kind"] == "reversal":
        original = lookup.get(line["reverses_line_id"])
        if original is not None:
            view["reversed_charge"] = {
                "line_id": original["line_id"],
                "station": original["station"],
                "month": original["month"],
                "amount_cents": original["amount_cents"],
                "reason": original.get("reason"),
            }
    return view


def show_month(archive_path, month):
    """Show the immutable original statement, correction rows and balance."""
    month = _months(month)
    with archive._locked(archive_path):
        archive._load_events(archive_path)
        ledger = _load_ledger(archive_path)
        statement = ledger["statements"].get(month)
        if statement is None:
            raise ValueError(f"month {month} is not closed")

        all_lines = []
        for stored in ledger["statements"].values():
            all_lines.extend(stored["lines"])
        for correction in ledger["corrections"]:
            all_lines.extend(correction["lines"])
        lookup = {line["line_id"]: line for line in all_lines}

        original_lines = [_line_view(line, lookup) for line in statement["lines"]]
        corrections = []
        correction_lines = []
        for correction in ledger["corrections"]:
            lines = [line for line in correction["lines"] if line["month"] == month]
            if not lines:
                continue
            corrections.append(
                {
                    "correction_id": correction["correction_id"],
                    "knowledge_seq": correction["knowledge_seq"],
                    "lines": [_line_view(line, lookup) for line in lines],
                }
            )
            correction_lines.extend(lines)

        stations = {}
        for line in statement["lines"]:
            entry = stations.setdefault(
                line["station"],
                {
                    "station": line["station"],
                    "original_amount_cents": 0,
                    "corrections_amount_cents": 0,
                    "balance_amount_cents": 0,
                    "original_charge_count": 0,
                    "correction_line_count": 0,
                },
            )
            entry["original_amount_cents"] += line["amount_cents"]
            entry["original_charge_count"] += 1
        for line in correction_lines:
            entry = stations.setdefault(
                line["station"],
                {
                    "station": line["station"],
                    "original_amount_cents": 0,
                    "corrections_amount_cents": 0,
                    "balance_amount_cents": 0,
                    "original_charge_count": 0,
                    "correction_line_count": 0,
                },
            )
            entry["corrections_amount_cents"] += line["amount_cents"]
            entry["correction_line_count"] += 1
        original_total = sum(line["amount_cents"] for line in statement["lines"])
        correction_total = sum(line["amount_cents"] for line in correction_lines)
        for entry in stations.values():
            entry["balance_amount_cents"] = (
                entry["original_amount_cents"] + entry["corrections_amount_cents"]
            )
        return {
            "month": month,
            "closed": True,
            "original_statement": {
                "month": statement["month"],
                "knowledge_seq": statement["knowledge_seq"],
                "lines": original_lines,
                "total_amount_cents": statement["total_amount_cents"],
            },
            "corrections": corrections,
            "balance": {
                "original_amount_cents": original_total,
                "corrections_amount_cents": correction_total,
                "amount_cents": original_total + correction_total,
                "by_station": [stations[station] for station in sorted(stations)],
            },
        }

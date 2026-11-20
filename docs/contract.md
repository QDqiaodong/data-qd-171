# Baggage reconciliation contract

## Single-shot reconciliation

Input has `flights`, `bags`, and `scans`. IDs are unique. Scan event order is `(event_time, scan_id)`; a scan for an unknown bag is rejected. Each bag's final scan determines current station. A bag is `not_seen` when it has no scans, `arrived` when its final station equals the flight destination, otherwise `in_transit`. A connection is at risk when the bag's latest station is the connecting airport and the next departure is earlier than `event_time + minimum_connection_minutes`. Times are ISO-8601 with explicit offsets. This tool reports operational evidence; it does not infer physical location or dispatch staff.

The single-shot CLI (`bag-reconcile --input INPUT --output OUTPUT`) is unchanged: it returns the original `bags` rows and `summary`, rejects malformed or duplicate-key JSON, and never overwrites an existing output file.

## Local verification archive

The archive lets staff reproduce, for any accepted `knowledge_seq`, exactly which scan evidence and flight plan versions the system used at that point. It runs entirely on the local machine using the standard library and never contacts an external service. It records operational evidence only; it does not infer physical baggage location and does not dispatch staff.

### Storage layout

An archive is a directory containing one append-only `events.jsonl` file; each line is one JSON event.

- The first line is the `initial` event at `knowledge_seq` 1. It stores the complete validated initial `flights`, `bags`, and `scans`. Initial records are treated as version 1 and their source `batch_id` is `null`.
- Every accepted batch appends one `batch` event carrying the next monotonic `knowledge_seq`, its unique `batch_id`, and the full scan/flight change lists. Nothing is ever overwritten: corrections, retractions, and flight revisions are appended as new events while prior payloads remain in the log.
- Sequence numbers are contiguous starting at 1. A rejected batch writes nothing and consumes no sequence number, so the archive stays byte-for-byte unchanged.
- The archive is reopened from disk on every command, so all historical versions remain queryable after the process restarts.

### `archive-init --input INPUT --archive ARCHIVE`

Initializes an archive from an existing single-shot input file. `ARCHIVE` must not already exist. The initial input is validated with the same rules as single-shot reconciliation (unique IDs, existing bag/flight references, explicit-offset timestamps, flight `arrival` not earlier than `departure`). On success the archive starts at `knowledge_seq` 1 and the command prints `{"knowledge_seq": 1}`.

### `archive-apply --archive ARCHIVE --batch BATCH`

Appends one batch file of the form `{"batch_id": "...", "scans": [...], "flights": [...]}`; `scans` and `flights` may be omitted and are treated as empty lists, but at least one list must contain a change. The whole batch is either accepted or rejected; partial acceptance is impossible because every change is validated before the event log is opened for appending.

Scan changes:

- `{"op": "append", "scan": <full scan>}` — the scan `id` must never have appeared in the archive before, including ids that were later retracted.
- `{"op": "replace", "scan_id": "...", "scan": <full scan>}` — replaces an active scan; the replacement must keep the same `id` and reference a known bag.
- `{"op": "retract", "scan_id": "..."}` — removes an active scan. A retracted id can never be reused.
- `replace` and `retract` require the target to be active at batch start.

Flight changes revise an existing `flight_id` (new flights cannot be introduced) with a complete `origin`, `destination`, `departure`, and `arrival`; the `flight_id` stays unchanged and `arrival` must not be earlier than `departure`.

Validation rules, any failure of which rejects the entire batch:

- non-empty unique `batch_id`; a repeated `batch_id` is rejected even when the payload is identical.
- each `scan_id` or `flight_id` appears at most once within the batch.
- all required fields are present, IDs and bag/flight references exist, and every timestamp carries an explicit UTC offset.
- bags and their flight assignments stay fixed at their initialized values; batches never modify bags.
- all scan and flight changes inside one batch share one new `knowledge_seq`.

On success the command prints the assigned sequence and batch id, e.g. `{"batch_id": "BATCH-1", "knowledge_seq": 2}`.

### `archive-report --archive ARCHIVE --as-of SEQ --output OUTPUT`

Generates the historical report for an accepted `knowledge_seq`. An invalid or never-accepted sequence is an error: no output file is created or overwritten. As with the single-shot command, an existing `OUTPUT` is never overwritten.

The report projects the event log up to and including `as-of` only, then runs the unchanged `reconcile` rules on that projected state. Later accepted changes can never alter an earlier sequence's result, and a report never mixes versions from two points in time.

Report layout:

```json
{
  "knowledge_seq": 2,
  "bags": [ ... identical bag rows and semantics as single-shot output ... ],
  "summary": { ... identical summary as single-shot output ... },
  "sources": {
    "scans": [
      {"scan_id": "S1", "knowledge_seq": 2, "batch_id": "BATCH-1"}
    ],
    "flights": [
      {"flight_id": "F1", "knowledge_seq": 1, "batch_id": null},
      {"flight_id": "F2", "knowledge_seq": 1, "batch_id": null}
    ]
  }
}
```

- `sources.scans` lists every scan that is **active at the requested sequence** — the set that participates in scan ordering — with the `knowledge_seq` and `batch_id` at which that version last took effect. Retracted scans are absent; an appended or replaced scan appears under its latest effective version.
- `sources.flights` lists every flight known at the requested sequence with the `knowledge_seq` and `batch_id` of its last effective revision.
- Initial versions always report `knowledge_seq` 1 and `batch_id` null.
- Querying the same sequence again yields the same business result and source identifiers, regardless of batches accepted afterwards.

### Worked acceptance sequence

With F1 arriving at BBB 11:00, F2 departing BBB 11:40, bag B assigned to F1 with `next_flight_id` F2, a 30-minute minimum connection, and B scanned at BBB at 10:55, the sequence-1 report has `connection_at_risk=false`. A batch correcting B's scan to 11:20 produces sequence 2 with `connection_at_risk=true`. A later batch revising F2's departure to 12:10 produces sequence 3 with `connection_at_risk=false`, while querying sequence 2 still returns `true` together with the scan and flight source versions used at that time.

### Errors

All four commands exit with status 2 and print `bag-reconcile: <message>` on validation, JSON, or filesystem errors. Archive commands never access external services and add no runtime dependencies beyond the standard library.

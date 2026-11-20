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

## Local loading-release workbench

A workbench is a second, independent state store that lives inside an existing
archive. It reconciles **manually declared** screening credentials with a
requested load manifest and turns an operator-confirmed manifest into an
exclusive release record. It processes only these declared records: it does not
infer physical bag location, does not dispatch staff, and never contacts an
external service. A "connection not at risk" report says nothing about
screening, so the loading decision is computed from screening credentials and
occupancy, never from `connection_at_risk`.

The workbench never rewrites `events.jsonl`; an existing archive needs no
rebuild. Scans and flights are read through the archive projection described
above, so every judgement binds to a reproducible `knowledge_seq`.

### Storage layout

The workbench is one JSON document, `<archive>/loading-workbench.json`, created
by `loading-init`. It is replaced atomically (write to a temporary file, fsync,
`os.replace`), so a reader always sees either the old or the new document, and a
filesystem error during a write leaves the previously accepted state intact and
readable. The workbench is created only in an existing, valid archive.

All workbench commands, and `archive-apply`, take the same archive-wide advisory
lock (`<archive>/.bag-reconcile.lock`). Each command therefore observes one
consistent snapshot of scans, flights, credentials and occupancy, and mutations
are serialised: concurrent commits of intersecting manifests either fully win or
fully lose, and a scan/flight read can never interleave with a half-written
occupancy update. Because the lock is an OS advisory lock, the ordering holds
across processes and after a restart; a crashed process's lock is released by
the kernel.

Python API (also standard-library only), in `baggage.loading`:
`init_workbench`, `apply_screening`, `review_manifest`, `commit_review`,
`cancel_manifest`, `manifest_status`, each taking the archive path (and the
batch/request file path where relevant).

### `loading-init --archive ARCHIVE`

Creates the single workbench for an existing archive. The archive must exist and
be valid, and the workbench must not already exist; otherwise the command is an
error. Every later loading command requires the workbench.

### `screening-apply --archive ARCHIVE --batch FILE`

Applies one batch of manual screening records and/or revocations, all-or-nothing.
Format:

```json
{"batch_id": "SCR-1", "operations": [
  {"op": "record", "clearance": {
     "id": "C1", "bag_id": "B1", "scan_id": "S1", "scan_knowledge_seq": 1,
     "decision": "pass",
     "valid_from": "2026-01-01T11:00:00+00:00",
     "valid_until": "2026-01-01T13:00:00+00:00"}},
  {"op": "revoke", "clearance_id": "C2"}
]}
```

- `operations` must be non-empty. `decision` is only `pass` or `fail`.
  `valid_from`/`valid_until` must carry an explicit offset and `valid_from` must
  be strictly before `valid_until`.
- `batch_id` and `clearance.id` are unique for the whole workbench lifetime,
  including after a revocation; a repeated `batch_id` is rejected even with an
  identical payload.
- A batch may reference each `bag_id` at most once. A `revoke`'s bag is the bag
  of the referenced record.
- On `record`, the bag must exist, `scan_id` must be that bag's **latest**
  archive scan at that moment, and `scan_knowledge_seq` must equal that scan
  version's source `knowledge_seq`. A wrong version is rejected even when the
  scan value is unchanged. Each bag has exactly one *current* credential: a new
  `record` replaces (supersedes) the bag's previous current record, but original
  records are retained for saved reviews.
- `revoke` may only cancel a bag's current, not-yet-revoked record; it never
  revives an older `pass`.
- Any invalid field/reference, a revoke of an unknown/superseded/revoked record,
  or a duplicate bag in the batch rejects the whole batch, leaving no partial
  record. A batch's validity does not depend on operation order within it.

Success prints `{"batch_id", "accepted": true, "archive_knowledge_seq",
"recorded": [...], "revoked": [...]}`.

### `loading-review --archive ARCHIVE --request FILE`

Saves one immutable review. Format:
`{"manifest_id": "M1", "flight_id": "F2", "bag_ids": ["B1", "B2"]}`. All IDs are
non-empty strings; `bag_ids` is non-empty, duplicate-free, and all known;
`flight_id` must be known. Malformed JSON, duplicate JSON keys, invalid types, or
unknown references are command errors and save nothing. A `manifest_id` that is
already registered or cancelled cannot start a new review. The same unregistered
`manifest_id` may yield several reviews, but at most one becomes its registered
basis. Array order does not affect the judgement; output bags are sorted by
`bag_id`.

A review uses one consistent archive+workbench snapshot, and the latest scan per
bag follows the existing `(event_time, scan_id)` order. A bag is **eligible**
only when all hold:

- the requested flight equals the bag's initialized `flight_id` or its non-empty
  `next_flight_id` (no new or modified assignments are introduced);
- the bag has a latest scan whose `station` equals the requested flight's
  `origin` and whose `event_time` is not later than the flight `departure`;
- the bag's current credential is not revoked, has `decision = pass`, and is
  bound to exactly the current latest scan's identity and effective source
  version;
- the requested flight's `departure` lies in `[valid_from, valid_until)`,
  compared as offset-aware instants (not strings, and never against the machine
  clock);
- the bag is not occupied by any other un-cancelled manifest in this workbench
  (occupancy is per `bag_id` and crosses flights).

A known but ineligible bag is **not** a command error: the review stores
`eligible=false` with distinguishable reasons such as `not_assigned_to_flight`,
`no_scan`, `station_mismatch`, `scan_after_departure`, `no_current_clearance`,
`clearance_revoked`, `clearance_not_pass`, `clearance_scan_stale`,
`clearance_window_not_covering_departure`, and `occupied`; multiple reasons that
hold are all listed, and `occupied_by` names the occupying manifest(s). "No
credential" and "credential pass but evidence version changed" are therefore
distinguishable. The review is `committable` only when every bag is eligible. It
records the requested flight's source version, each bag's latest scan with its
source version/batch, and the credential record and status at that time — real
values, not booleans or hashes. Initial sources report `batch_id: null`. A review
occupies nothing.

Success prints the stored review including `review_id`.

### `loading-commit --archive ARCHIVE --review ID`

Registers the whole reviewed manifest atomically and claims all its bags; a
partial registration is impossible. It accepts only reviews created as
`committable`; a review that was ineligible then cannot be upgraded by later
facts.

Scans, flight corrections, and screening batches may arrive between review and
commit, so the commit re-verifies that the review is still supported by the same
relevant evidence and re-checks occupancy. Relevant evidence is exactly: the
requested flight's effective source version, each requested bag's latest scan
identity and effective source version, and each bag's current credential
identity and record/revocation status. A corrected relevant version invalidates
the review even if its value is unchanged. Changes to unrelated flights or bags,
or a newly added/corrected earlier scan that is not some bag's latest scan, do
not invalidate it merely because the global `knowledge_seq` or workbench counter
advanced.

The commit is rejected — leaving occupancy and registrations untouched — if
relevant evidence changed, any bag is already occupied by another manifest, or
the `manifest_id` was already registered by another review. Only a fresh review
confirms new evidence. On success, the full immutable release basis and all bag
claims are written in one atomic step, with no visible state where a manifest
exists without its claims or only some bags are claimed.

Success prints a receipt. Retrying `loading-commit` for the **same** `review_id`
after a successful commit returns the first receipt and never commits again or
re-claims bags — even after a later revocation or cancellation; after a
cancellation it still returns the original receipt and does not restore
occupancy. A different `review_id` cannot reuse that `manifest_id` to overwrite
the earlier manifest.

### `loading-cancel --archive ARCHIVE --manifest ID`

Cancels a registered manifest and releases all its claims. Cancelling the same
manifest again succeeds with no further effect. A cancelled `manifest_id` can
never be used for a new registration.

### `loading-status --archive ARCHIVE --manifest ID`

Returns the stored original review basis, the registration result, and a status
re-checked against current evidence (ignoring this manifest's own occupancy so
it never conflicts with itself; original basis and current re-check are clearly
separate fields):

- `active` — all relevant evidence is unchanged and still satisfied;
- `suspended` — a relevant evidence version changed or a credential was revoked,
  or a condition is no longer met; the manifest **still occupies** all its bags
  until an explicit `loading-cancel`, and the original basis is never rewritten;
- `cancelled` — after `loading-cancel`.

Unknown `review`/`manifest` IDs error. Status reports the unmet conditions or
changed sources and still-occupying manifest IDs.

### Loading errors and invariants

Workbench commands exit with status 2 and print `bag-reconcile: <message>` on
validation, JSON, duplicate-key, or filesystem errors. All constraints hold
across process restarts; state is read from disk on every command.

## Offline per-desk evidence handoff

Handoff moves one desk's operational evidence from a sender that owns an
archive to a receiver that never reads it. The two sides exchange ordinary
JSON files by any manual means (e-mail, removable media, copy); nothing here
signs, contacts a network, or authenticates the operator. A packet is a
self-contained description of **which evidence the desk is responsible for
viewing** — it never asserts that a bag is physically present, never triggers
dispatch or loading, and never carries screening records or load manifests.

### Streams and scope

A delivery stream is identified by a sender-chosen, unique `stream_id` and a
fixed station code `desk`. The same archive may host several independent
streams; each advances on its own. A registered `stream_id` can never change
station or be reused, and a receiver inbox carries exactly one stream.

At a given archive cutoff the desk's scope is decided by the **effective flight
plan**. A bag is in scope only when it has a non-empty `next_flight_id` whose
flight `origin` equals the desk. Every in-scope bag contributes one row keyed by
`bag_id` that carries:

- the bag's `next_flight_id`;
- that flight's `origin`, `destination`, `departure`, `arrival`, and the
  flight version's source `knowledge_seq` (`flight_knowledge_seq`);
- the bag's latest **active scan at this desk**, ordered by the real
  offset-aware `event_time` and then `scan_id`, with its `scan_id`,
  `event_time`, and source `knowledge_seq` — or `null` when the bag has no scan
  at this desk.

Scans are filtered by station *before* the latest is chosen, so a later scan at
another station never displaces this desk's evidence. Prior flight legs are
never delivered; only the next flight is used for scope and content.

### Storage and durability

- The sender keeps `<archive>/handoff-sender.json`: every stream's desk,
  confirmed cutoff, next packet number, the one packet awaiting
  acknowledgement, and the identity of every confirmed packet.
- A receiver keeps `<inbox>/handoff-inbox.json`: its stream/desk, the applied
  packet number, the confirmed cutoff, the effective row copy, and every
  applied packet's identity and receipt.
- Both documents are replaced atomically (temp file, fsync, `os.replace`), so a
  reader always sees the old or the new state and an interrupted write leaves
  the previous accepted state readable.
- The sender reads and writes its document under the archive-wide advisory
  lock; the receiver serialises imports on `<inbox>/.handoff.lock`. Both are OS
  advisory locks, so ordering holds across processes and after a restart.
- The archive's `events.jsonl` is never modified. Projections use the existing
  `archive._project` rules, so every packet binds to reproducible archive
  versions.

### Packet format

```json
{
  "format": "bag-reconcile-handoff/v1",
  "stream_id": "STREAM-BBB",
  "desk": "BBB",
  "packet_no": 1,
  "prev_packet_no": 0,
  "from_seq": 0,
  "to_seq": 1,
  "changes": [
    {"bag_id": "B", "next_flight_id": "F2", "origin": "BBB",
     "destination": "CCC", "departure": "2026-01-01T12:00:00+00:00",
     "arrival": "2026-01-01T13:00:00+00:00", "flight_knowledge_seq": 1,
     "latest_scan": {"scan_id": "S-BBB",
                     "event_time": "2026-01-01T11:10:00+00:00",
                     "knowledge_seq": 1}}
  ],
  "retractions": ["B"],
  "integrity": {"algorithm": "sha256", "value": "<64 lowercase hex>"}
}
```

- `packet_no` starts at 1 and is contiguous per stream. `prev_packet_no` is the
  previous packet's number (0 for the first).
- `from_seq` is the stream's confirmed cutoff the packet builds on; `to_seq` is
  the new cutoff. Both are accepted archive `knowledge_seq` values and
  `to_seq > from_seq`.
- `changes` holds the full replacement row for every bag newly in scope or whose
  any delivered field differs from the confirmed cutoff. `retractions` lists the
  `bag_id`s that were delivered before but are no longer in scope; it carries no
  new destination. A row still in scope whose scan was withdrawn stays in
  `changes` with the updated (possibly `null`) `latest_scan`; it is never
  retracted. Unchanged rows are not resent, so a packet with no changes and no
  retractions is valid and proves the desk has reached `to_seq`.
- `integrity.value` is the lowercase SHA-256 of the UTF-8 encoding of
  `json.dumps(body, sort_keys=True, separators=(",", ":"))`, where `body` is the
  whole packet without the `integrity` field. It is a plain integrity check an
  offline receiver can recompute; it is not a signature.

Scope is a **net** comparison between two cutoffs: packets may skip intermediate
archive sequences, and only the state at `from_seq` versus `to_seq` matters.

### Sender commands

- `handoff-open --archive ARCHIVE --stream ID --desk STATION` — registers one
  sender stream beside an existing archive. The `stream_id` must be new; the
  station is fixed for its lifetime. Prints the registration.
- `handoff-export --archive ARCHIVE --stream ID --as-of SEQ --output PACKET` —
  exports the next packet, or re-exports the one awaiting acknowledgement.
  - `SEQ` must be an accepted archive sequence strictly greater than the
    confirmed cutoff.
  - If a packet is awaiting acknowledgement, only its cutoff may be exported;
    any other cutoff is refused. Re-exporting to a new path yields byte-identical
    content.
  - An existing `PACKET` path is never overwritten. The packet is written and
    fsynced to a temporary file and atomically linked into place, so a failed or
    interrupted export leaves no half packet. The pending packet is recorded
    durably **before** the file becomes visible, so a restart still knows it is
    awaiting a receipt; if publishing fails before anything is visible, the
    pending record is rolled back and no cutoff or number advances.
  - At most one packet per stream awaits acknowledgement at a time. Other
    streams and the archive itself keep advancing.
- `handoff-ack --archive ARCHIVE --stream ID --receipt RECEIPT` — advances a
  stream only on a receipt that exactly matches its pending packet (stream, desk,
  packet number, both cutoffs, and the packet's content digest). A receipt for a
  different stream, different content, or a packet never sent is refused and the
  position is unchanged. A genuine receipt for an already-confirmed packet is
  accepted but never advances or rolls back the stream, even if a later packet is
  already pending. Prints the confirmed cutoff and whether it advanced.

### Receiver commands

- `handoff-inbox --inbox INBOX --stream ID --desk STATION` — creates an empty
  receiver for exactly one stream, without needing the source archive. `INBOX`
  must not already exist. An empty inbox reports packet number and cutoff 0.
- `handoff-import --inbox INBOX --packet PACKET` — validates and applies one
  packet, printing the receipt to stdout for the operator to pass back. The
  receipt is:

  ```json
  {"format": "bag-reconcile-handoff-receipt/v1", "stream_id": "...",
   "desk": "...", "packet_no": 1, "from_seq": 0, "to_seq": 1,
   "packet_sha256": "<64 lowercase hex>"}
  ```

  Validation order:

  1. Packet identity and format: exact field set, `format`, ids, integer
     ranges, `to_seq > from_seq`, and every row's `origin` equals the desk.
  2. Integrity: the recomputed SHA-256 must equal `integrity.value`.
  3. Stream/desk match against this inbox.
  4. Retry rule (before the continuation check): if `packet_no` was already
     applied, return its first receipt when the content digest matches, or
     reject when the number was applied with different content. This never
     re-applies and never rewinds the copy.
  5. Continuation: `packet_no` must equal the current number + 1,
     `prev_packet_no` must equal the current number, and `from_seq` must equal
     the inbox's confirmed cutoff. A skipped/out-of-order number, a wrong base,
     or a mismatched `prev_packet_no` rejects the whole packet.
  6. Content: no duplicate `bag_id` within `changes` or `retractions`, and every
     retraction must name a row the inbox currently holds.

  A rejected packet leaves the copy unchanged. An accepted packet updates the
  row copy, the cutoff, and the resendable receipt in a single atomic write.
- `handoff-view --inbox INBOX` — prints the effective packet number, confirmed
  cutoff, and all of this desk's rows, sorted by `bag_id`. An empty inbox reports
  `packet_no` 0, `confirmed_seq` 0, and no rows.

### Recovery after restart or failure

Every command re-reads its state from disk, so closing and relaunching the
program loses nothing.

- **Export failed or was interrupted.** If no packet became visible, nothing
  was consumed: export the same or another cutoff normally. If the packet did
  become visible, it is remembered as pending; re-export the same cutoff (bytes
  unchanged) or wait for its receipt.
- **Sender restarted while a packet is pending.** The pending packet is still
  recorded; re-export its cutoff or apply the matching receipt.
- **Receiver applied a packet but the receipt was lost.** Re-import the same
  packet (from either the original or a re-exported copy); it returns the first
  receipt without re-applying.
- **Receiver update failed.** The previous accepted copy remains readable; the
  same packet can be imported again and applied once.
- **Sender lost a confirmation but the receiver has applied it.** The sender
  stays at its old cutoff until it receives the receipt. If the packet is still
  pending, re-send the same cutoff. If multiple older packets exist, each
  re-import still returns its own first receipt.

### Errors

All six commands exit with status 2 and print `bag-reconcile: <message>` on
validation, JSON, duplicate-key, or filesystem errors. No command contacts an
external service; everything uses the standard library.

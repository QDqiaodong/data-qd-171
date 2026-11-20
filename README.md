# data-qd-171 | Airport baggage reconciliation

Reconciles synthetic baggage scans with a flight manifest and reports each bag's latest known station, missing scans, and missed-connection risk. Python 3.11+; standard library only.

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
.venv/bin/bag-reconcile --input fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

The fixture is synthetic. `docs/contract.md` defines ordering and connection rules. The CLI rejects malformed or duplicate-key JSON and does not overwrite an existing output. MIT licensed.

Fixture summary: 3 bags, 2 arrived, 1 in transit, and 1 connection risk.

## Local verification archive

The archive keeps every accepted scan/flight change locally so a historical decision can be reproduced at any `knowledge_seq`. It is standard-library only and never calls an external service; it reports evidence only and never dispatches staff.

```sh
bag-reconcile archive-init   --input INPUT  --archive ARCHIVE
bag-reconcile archive-apply  --archive ARCHIVE --batch BATCH
bag-reconcile archive-report --archive ARCHIVE --as-of SEQ --output OUTPUT
```

- `archive-init` requires a non-existing `ARCHIVE` path and starts the log at `knowledge_seq` 1 with the initial input as version 1 (source `batch_id` null).
- `archive-apply` validates the whole batch first; any invalid field, ID, reference, timestamp, or time ordering rejects the entire batch without touching the archive and without consuming a sequence. Scan ops are `append` / `replace` / `retract`; flight changes revise an existing `flight_id` in place by appending a new version. `batch_id` values are unique and retracted scan ids can never be reused.
- `archive-report` runs the unchanged reconcile rules against the state known at `as-of`, binds the result to that sequence, and lists each active scan and flight with the `knowledge_seq` and `batch_id` where its version last took effect. Unknown sequences error out and never create the output file; existing outputs are never overwritten.

See `docs/contract.md` for the event-log and report layouts and the full validation rules.

## Local loading-release workbench

The workbench reconciles manually declared screening credentials with a requested
load manifest and registers an operator-confirmed manifest as an exclusive
release record. It lives inside an existing archive, never rewrites
`events.jsonl`, and needs no archive rebuild. It reports declared evidence only:
it never infers physical bag location, dispatches staff, or calls an external
service, and it does not treat `connection_at_risk` as a screening result.

```sh
bag-reconcile loading-init    --archive ARCHIVE
bag-reconcile screening-apply --archive ARCHIVE --batch BATCH
bag-reconcile loading-review  --archive ARCHIVE --request REQUEST
bag-reconcile loading-commit  --archive ARCHIVE --review REVIEW_ID
bag-reconcile loading-cancel  --archive ARCHIVE --manifest MANIFEST_ID
bag-reconcile loading-status  --archive ARCHIVE --manifest MANIFEST_ID
```

- `loading-init` creates the single workbench file `loading-workbench.json`
  inside a valid, existing archive.
- `screening-apply` accepts a whole batch of manual `record`/`revoke`
  operations or none; credentials bind to a bag's latest scan identity and
  source `knowledge_seq`, and `batch_id`/`clearance.id` are never reused.
- `loading-review` saves an immutable review (occupying nothing) with
  per-bag eligibility, reasons, occupiers, and the source versions used.
- `loading-commit` atomically registers the whole manifest and claims its bags,
  re-verifying that the review's relevant evidence is unchanged. Retries of the
  same review return the original receipt.
- `loading-cancel` releases all claims; repeated cancels are no-ops.
- `loading-status` reports `active`, `suspended`, or `cancelled` from current
  evidence while keeping the original basis separate.

All loading commands share one archive-wide lock with `archive-apply`, so scan/
flight reads never mix with a half-written occupancy update and concurrent
commits of intersecting manifests are serialised. See `docs/contract.md` for the
formats, reasons, and invariants.

## Local sample-release workspace and contamination tracking

The study workspace sits beside an existing archive and publishes named, frozen
sample releases built from manual `success`/`failure` labels and declared
anomaly links. It then reports, for any of its own revisions, which released
rows remain usable and which must be withdrawn — with re-checkable evidence.
It is offline bookkeeping only: it never treats archive verification as truth,
never trains a model, and never drives scheduling or loading. `events.jsonl` is
never modified.

```sh
bag-reconcile study-init    --archive ARCHIVE
bag-reconcile study-apply   --archive ARCHIVE --batch FILE
bag-reconcile study-publish --archive ARCHIVE --request FILE
bag-reconcile study-view    --archive ARCHIVE [--release ID] [--as-of REV]
```

- A `bag_id` is a stable sample identity; the workspace keeps its own
  `study_rev`, starting at 0 and advancing by 1 per accepted batch or publish.
- `study-apply` takes manual labels (`success`/`failure`/`null` retraction, each
  pinned to an accepted `features_as_of`) and undirected `link`/`unlink`
  relations; connected bags form one anomaly group. The whole batch is accepted
  or rejected; batch ids are never reused.
- `study-publish` freezes each row's identity, partition, label version, and the
  archive's `status`/`last_scan`/`connection_at_risk` plus scan and flight source
  versions at that label's cutoff. It refuses to split or re-expose a group:
  never-exposed groups pick any partition, a group already in one partition may
  only repeat it, and a group in both may not be released. Replaying the same
  request returns the first frozen release.
- `study-view` reports usable/withdrawn rows and reasons (`label_version_changed`,
  `group_exposure_conflict`) with, when contaminated, each side's real exposure
  and a link chain to it. Historical revisions and frozen releases are never
  rewritten.

See `docs/contract.md` for the full formats, partition rules, and evidence
layout.

## Offline desk handoff

Handoff delivers one desk's evidence to a receiver that never reads the source
archive, through plain JSON files moved by hand. Each sender stream is a fixed
`stream_id` + station `desk`; a receiver inbox carries exactly one stream. A
packet carries only bags whose next flight departs from the desk, with that
desk's latest scan (never another station's scans, earlier legs, screening
records, or manifests), as the net change since the last confirmed cutoff.

```sh
bag-reconcile handoff-open   --archive ARCHIVE --stream ID --desk STATION
bag-reconcile handoff-export --archive ARCHIVE --stream ID --as-of SEQ --output PACKET
bag-reconcile handoff-inbox  --inbox INBOX --stream ID --desk STATION
bag-reconcile handoff-import --inbox INBOX --packet PACKET   # prints a receipt
bag-reconcile handoff-ack    --archive ARCHIVE --stream ID --receipt RECEIPT
bag-reconcile handoff-view   --inbox INBOX
```

Packets are numbered per stream from 1, carry `from_seq`/`to_seq` cutoffs,
full replacement rows, retraction-only entries, and a SHA-256 integrity value.
Exactly one packet per stream awaits acknowledgement; an already-applied packet
re-imports to its first receipt without rewinding, and a sender advances only on
a receipt that exactly matches a sent packet. State is written atomically, so
every side can be closed and relaunched without losing a pending packet, an
applied copy, or a receipt. See `docs/contract.md` for the file formats,
validation order, and restart recovery steps.

## Local monthly evidence-fee settlement

Monthly settlement records a fixed evidence service fee per transfer service
whose documentary record satisfies the contract, and issues immutable monthly
statements that later corrections adjust without rewriting. It lives inside an
existing archive, never modifies `events.jsonl`, and needs no rebuild. Amounts
record evidence only: they never prove a physical transfer and never affect
screening, loading, or handoff.

```sh
bag-reconcile fee-init    --archive ARCHIVE --rates RATES
bag-reconcile fee-preview --archive ARCHIVE --month YYYY-MM --as-of SEQ
bag-reconcile fee-close   --archive ARCHIVE --month YYYY-MM --as-of SEQ
bag-reconcile fee-correct --archive ARCHIVE --as-of SEQ
bag-reconcile fee-show    --archive ARCHIVE --month YYYY-MM
```

- `fee-init` stores a strict station→positive-integer-cents rate table and an
  empty ledger; it refuses an archive that already has one.
- `fee-preview` is read-only: it lists every candidate for the month at the
  cutoff (billable or not) with reasons, both flight source versions, the
  adopted scan and its version, and per-station billable counts and amounts. It
  may use any accepted cutoff, including one older than the last posted one.
- `fee-close` first-closes one month (all stations, even if empty) and, in the
  same atomic step, registers any corrections now due to older closed months.
  Corrections are never skipped because the new month has no fees.
- `fee-correct` rechecks all closed months at a cutoff and posts only real
  financial changes; repeating a cutoff adds nothing.
- `fee-show` prints a closed month's immutable original statement, its
  correction lines (with the charges they reverse), and the per-station balance.

A service whose attribution (station/month) or billability actually changes is
reversed at its old station/month and, only if the new month is already closed,
charged again with a fresh positive line. Evidence-version changes that keep the
same station, month, and amount add nothing. Write cutoffs must be accepted and
never older than the last processed one. The ledger is written atomically and
re-read on every command, so a failed or interrupted run posts nothing and can
be retried exactly once. See `docs/contract.md` for the full rules.

## Local read-audit reconciliation

`read-audit` reconciles one station's imperfect barcode reads with the bags that
must be checked for a chosen next leg, and reports how many bags can be given
ownership evidence, which bags certainly lack evidence, and which reads still
need a human to look at the original image. It is read-only, keeps no state, and
never touches `events.jsonl`.

```sh
bag-reconcile read-audit --archive ARCHIVE --as-of SEQ --input BATCH \
    [--decisions FILE] --output REPORT
```

- `BATCH` gives `station`, a half-open departure window, a half-open capture
  window, and `reads` (`read_id`, `captured_at`, `candidate_bag_ids`). Only
  offset-aware instants are compared; candidate ids that are unknown or out of
  scope are kept verbatim but excluded from reconciliation.
- The optional `--decisions` file carries human `confirm` (`read_id`, `bag_id`,
  `proof_ref`) and `discard` (`read_id`, `proof_ref`) decisions. They are
  binding and all-or-nothing: any conflict rejects the whole request.
- The report gives `max_coverage`, `min_missing`, one example pairing, and — for
  every checked bag and every in-window read — the conclusions that hold across
  *all* maximum-coverage schemes (`certain_evidence` / `possible_missing` /
  `certain_missing`, and `always_used` / `optional` / `unused`), while
  distinguishing confirmed, discarded, and naturally unattributable reads.

An existing output is never overwritten, and a failed run creates no file. See
`docs/contract.md` for the exact formats and the full conclusion semantics. Use
`tools/read_audit_load.py` and `tools/read_audit_bench.py` to reproduce the
single-core load and measurement.

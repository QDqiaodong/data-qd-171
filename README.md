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

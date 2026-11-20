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

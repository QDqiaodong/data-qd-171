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

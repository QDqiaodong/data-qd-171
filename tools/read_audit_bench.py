"""Reproducible read-audit benchmark (standard library only).

Generates a synthetic load, then runs one full ``read-audit`` build in a fresh
subprocess and reports wall-clock time and peak resident set size.  Pin it to a
single core with ``taskset`` for the contract's single-core figure::

    python3 tools/read_audit_bench.py --work /tmp/readaudit-bench --pin 0

Peak memory is read from ``resource.getrusage(RUSAGE_SELF).ru_maxrss`` inside
the timed child on Linux, which is KiB.  The full per-bag and per-read report is
written to disk, so the measurement is not achieved by omitting conclusions.
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _prepare(work, bags, reads, candidates, share):
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable, str(ROOT / "tools" / "read_audit_load.py"),
            "--out", str(work),
            "--bags", str(bags), "--reads", str(reads),
            "--candidates", str(candidates),
            "--component-share", str(share),
        ],
        check=True, stdout=subprocess.DEVNULL,
    )
    return work


def _build_archive(work):
    archive = work / "archive"
    if archive.exists():
        import shutil
        shutil.rmtree(archive)
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]);"
         "from baggage import archive;"
         "print(archive.init_archive(sys.argv[2], sys.argv[3]))",
         str(ROOT), str(work / "initial.json"), str(archive)],
        check=True, stdout=subprocess.DEVNULL, env=env,
    )
    return archive


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", default="/tmp/readaudit-bench")
    parser.add_argument("--bags", type=int, default=4500)
    parser.add_argument("--reads", type=int, default=4500)
    parser.add_argument("--candidates", type=int, default=36000)
    parser.add_argument("--component-share", type=float, default=0.9)
    parser.add_argument("--pin", type=int, default=None,
                        help="optional CPU index to pin the timed run to")
    parser.add_argument("--no-decisions", action="store_true")
    args = parser.parse_args(argv)

    work = _prepare(args.work, args.bags, args.reads, args.candidates,
                    args.component_share)
    archive = _build_archive(work)
    output = work / "report.json"
    if output.exists():
        output.unlink()

    command = [
        sys.executable, "-I", "-c",
        "import sys, resource, json; sys.path.insert(0, sys.argv[1]);"
        "from baggage import readaudit;"
        "readaudit.write_report(sys.argv[2], 1, sys.argv[3], sys.argv[4],"
        " sys.argv[5] if sys.argv[5] else None);"
        "print(json.dumps(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))",
        str(ROOT), str(archive), str(work / "batch.json"), str(output),
        "" if args.no_decisions else str(work / "decisions.json"),
    ]
    if args.pin is not None:
        command = ["taskset", "-c", str(args.pin)] + command

    started = time.perf_counter()
    completed = subprocess.run(
        command, check=True, capture_output=True, text=True
    )
    elapsed = time.perf_counter() - started
    child_peak = int(completed.stdout.strip().splitlines()[-1])

    report = json.loads(output.read_text(encoding="utf-8"))
    summary = report["summary"]
    result = {
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
        },
        "params": {
            "bags": args.bags,
            "reads": args.reads,
            "candidates": args.candidates,
            "component_share": args.component_share,
            "decisions": not args.no_decisions,
            "pinned_cpu": args.pin,
        },
        "result": {
            "scoped_bag_count": summary["scoped_bag_count"],
            "max_coverage": summary["max_coverage"],
            "min_missing": summary["min_missing"],
            "report_bag_rows": len(report["bags"]),
            "report_read_rows": len(report["reads"]),
            "report_bytes": output.stat().st_size,
        },
        "timing_seconds": round(elapsed, 3),
        "peak_rss_kib": child_peak,
        "peak_rss_mib": round(child_peak / 1024.0, 1),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


if __name__ == "__main__":
    main()

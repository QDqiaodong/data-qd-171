import argparse
import json
import sys
from pathlib import Path

from . import archive, fees, handoff, loading, readaudit, study
from .core import reconcile


def _pairs(items):
    return archive._pairs(items)


ARCHIVE_COMMANDS = ("archive-init", "archive-apply", "archive-report")
LOADING_COMMANDS = (
    "loading-init",
    "screening-apply",
    "loading-review",
    "loading-commit",
    "loading-cancel",
    "loading-status",
)
HANDOFF_COMMANDS = (
    "handoff-open",
    "handoff-export",
    "handoff-inbox",
    "handoff-import",
    "handoff-ack",
    "handoff-view",
)
FEE_COMMANDS = (
    "fee-init",
    "fee-preview",
    "fee-close",
    "fee-correct",
    "fee-show",
)
STUDY_COMMANDS = (
    "study-init",
    "study-apply",
    "study-publish",
    "study-view",
)
READ_AUDIT_COMMANDS = ("read-audit",)


def _main_legacy(argv):
    parser = argparse.ArgumentParser(prog="bag-reconcile")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    source = json.loads(Path(args.input).read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    result = reconcile(source)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps(result["summary"], sort_keys=True))


def _main_archive(command, argv):
    parser = argparse.ArgumentParser(prog=f"bag-reconcile {command}")
    if command == "archive-init":
        parser.add_argument("--input", required=True)
        parser.add_argument("--archive", required=True)
        args = parser.parse_args(argv)
        seq = archive.init_archive(args.input, args.archive)
        print(json.dumps({"knowledge_seq": seq}, sort_keys=True))
    elif command == "archive-apply":
        parser.add_argument("--archive", required=True)
        parser.add_argument("--batch", required=True)
        args = parser.parse_args(argv)
        seq, batch_id = archive.apply_batch(args.archive, args.batch)
        print(json.dumps({"knowledge_seq": seq, "batch_id": batch_id}, sort_keys=True))
    else:
        parser.add_argument("--archive", required=True)
        parser.add_argument("--as-of", required=True, type=int)
        parser.add_argument("--output", required=True)
        args = parser.parse_args(argv)
        report = archive.write_report(args.archive, args.as_of, args.output)
        print(json.dumps(report["summary"], sort_keys=True))


def _main_loading(command, argv):
    parser = argparse.ArgumentParser(prog=f"bag-reconcile {command}")
    parser.add_argument("--archive", required=True)
    if command == "loading-init":
        args = parser.parse_args(argv)
        result = loading.init_workbench(args.archive)
    elif command == "screening-apply":
        parser.add_argument("--batch", required=True)
        args = parser.parse_args(argv)
        result = loading.apply_screening(args.archive, args.batch)
    elif command == "loading-review":
        parser.add_argument("--request", required=True)
        args = parser.parse_args(argv)
        result = loading.review_manifest(args.archive, args.request)
    elif command == "loading-commit":
        parser.add_argument("--review", required=True)
        args = parser.parse_args(argv)
        result = loading.commit_review(args.archive, args.review)
    elif command == "loading-cancel":
        parser.add_argument("--manifest", required=True)
        args = parser.parse_args(argv)
        result = loading.cancel_manifest(args.archive, args.manifest)
    else:
        parser.add_argument("--manifest", required=True)
        args = parser.parse_args(argv)
        result = loading.manifest_status(args.archive, args.manifest)
    print(json.dumps(result, sort_keys=True))


def _main_handoff(command, argv):
    parser = argparse.ArgumentParser(prog=f"bag-reconcile {command}")
    if command == "handoff-open":
        parser.add_argument("--archive", required=True)
        parser.add_argument("--stream", required=True)
        parser.add_argument("--desk", required=True)
        args = parser.parse_args(argv)
        result = handoff.open_stream(args.archive, args.stream, args.desk)
    elif command == "handoff-export":
        parser.add_argument("--archive", required=True)
        parser.add_argument("--stream", required=True)
        parser.add_argument("--as-of", required=True, type=int)
        parser.add_argument("--output", required=True)
        args = parser.parse_args(argv)
        result = handoff.export_packet(
            args.archive, args.stream, args.as_of, args.output
        )
    elif command == "handoff-inbox":
        parser.add_argument("--inbox", required=True)
        parser.add_argument("--stream", required=True)
        parser.add_argument("--desk", required=True)
        args = parser.parse_args(argv)
        result = handoff.create_inbox(args.inbox, args.stream, args.desk)
    elif command == "handoff-import":
        parser.add_argument("--inbox", required=True)
        parser.add_argument("--packet", required=True)
        args = parser.parse_args(argv)
        result = handoff.import_packet(args.inbox, args.packet)
    elif command == "handoff-ack":
        parser.add_argument("--archive", required=True)
        parser.add_argument("--stream", required=True)
        parser.add_argument("--receipt", required=True)
        args = parser.parse_args(argv)
        result = handoff.acknowledge(args.archive, args.stream, args.receipt)
    else:
        parser.add_argument("--inbox", required=True)
        args = parser.parse_args(argv)
        result = handoff.view_inbox(args.inbox)
    print(json.dumps(result, sort_keys=True))


def _main_fee(command, argv):
    parser = argparse.ArgumentParser(prog=f"bag-reconcile {command}")
    parser.add_argument("--archive", required=True)
    if command == "fee-init":
        parser.add_argument("--rates", required=True)
        args = parser.parse_args(argv)
        result = fees.init_ledger(args.archive, args.rates)
    elif command == "fee-preview":
        parser.add_argument("--month", required=True)
        parser.add_argument("--as-of", required=True, type=int)
        args = parser.parse_args(argv)
        result = fees.preview_month(args.archive, args.month, args.as_of)
    elif command == "fee-close":
        parser.add_argument("--month", required=True)
        parser.add_argument("--as-of", required=True, type=int)
        args = parser.parse_args(argv)
        result = fees.close_month(args.archive, args.month, args.as_of)
    elif command == "fee-correct":
        parser.add_argument("--as-of", required=True, type=int)
        args = parser.parse_args(argv)
        result = fees.correct_ledger(args.archive, args.as_of)
    else:
        parser.add_argument("--month", required=True)
        args = parser.parse_args(argv)
        result = fees.show_month(args.archive, args.month)
    print(json.dumps(result, sort_keys=True))


def _main_study(command, argv):
    parser = argparse.ArgumentParser(prog=f"bag-reconcile {command}")
    parser.add_argument("--archive", required=True)
    if command == "study-init":
        args = parser.parse_args(argv)
        result = study.init_study(args.archive)
    elif command == "study-apply":
        parser.add_argument("--batch", required=True)
        args = parser.parse_args(argv)
        result = study.apply_study(args.archive, args.batch)
    elif command == "study-publish":
        parser.add_argument("--request", required=True)
        args = parser.parse_args(argv)
        result = study.publish_release(args.archive, args.request)
    else:
        parser.add_argument("--release")
        parser.add_argument("--as-of", type=int)
        args = parser.parse_args(argv)
        result = study.view_study(args.archive, args.release, args.as_of)
    print(json.dumps(result, sort_keys=True))


def _main_read_audit(command, argv):
    parser = argparse.ArgumentParser(prog=f"bag-reconcile {command}")
    parser.add_argument("--archive", required=True)
    parser.add_argument("--as-of", required=True, type=int)
    parser.add_argument("--input", required=True)
    parser.add_argument("--decisions")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    report = readaudit.write_report(
        args.archive, args.as_of, args.input, args.output, args.decisions
    )
    print(json.dumps(report["summary"], sort_keys=True))


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)
    command = argv[0] if argv else None
    try:
        if command in ARCHIVE_COMMANDS:
            _main_archive(command, argv[1:])
        elif command in LOADING_COMMANDS:
            _main_loading(command, argv[1:])
        elif command in HANDOFF_COMMANDS:
            _main_handoff(command, argv[1:])
        elif command in FEE_COMMANDS:
            _main_fee(command, argv[1:])
        elif command in STUDY_COMMANDS:
            _main_study(command, argv[1:])
        elif command in READ_AUDIT_COMMANDS:
            _main_read_audit(command, argv[1:])
        else:
            _main_legacy(argv)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"bag-reconcile: {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

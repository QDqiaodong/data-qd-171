import argparse
import json
import sys
from pathlib import Path

from . import archive
from .core import reconcile


def _pairs(items):
    return archive._pairs(items)


ARCHIVE_COMMANDS = ("archive-init", "archive-apply", "archive-report")


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


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)
    command = argv[0] if argv else None
    try:
        if command in ARCHIVE_COMMANDS:
            _main_archive(command, argv[1:])
        else:
            _main_legacy(argv)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"bag-reconcile: {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

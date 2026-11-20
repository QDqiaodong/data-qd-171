"""Synthetic read-audit load generator (standard library only).

    python3 tools/read_audit_load.py --out DIR [--bags 4500] [--reads 4500] \
        [--candidates 36000] [--component-share 0.9]

Writes ``initial.json`` (archive input), ``batch.json`` (read BATCH), and
``decisions.json`` (a small, valid decision file) into ``out`` so the read-audit
benchmark can be reproduced.  Most candidates are placed in ONE connected
dispute component by giving each read a sliding window of consecutive bags.
"""

import argparse
import json
import random
from pathlib import Path

STATION = "BBB"
PRIOR = "F-PRIOR"
NEXT = "F-NEXT"
DEPART = "2026-03-01T11:00:00+00:00"
ARRIVE = "2026-03-01T12:30:00+00:00"


def build(bags, reads, candidates, component_share, seed):
    rng = random.Random(seed)
    bag_ids = [f"B{i:05d}" for i in range(bags)]
    flights = [
        {"id": PRIOR, "origin": "AAA", "destination": STATION,
         "departure": "2026-03-01T07:00:00+00:00",
         "arrival": "2026-03-01T08:30:00+00:00"},
        {"id": NEXT, "origin": STATION, "destination": "CCC",
         "departure": DEPART, "arrival": ARRIVE},
    ]
    bag_rows = [
        {"id": bag_id, "flight_id": PRIOR, "next_flight_id": NEXT,
         "minimum_connection_minutes": 30}
        for bag_id in bag_ids
    ]

    base = candidates // reads
    extra = candidates - base * reads
    window = max(1, base)
    big_count = int(reads * component_share)
    read_rows = []
    for index in range(reads):
        size = base + (1 if index < extra else 0)
        if index < big_count:
            # Sliding window over the bag ring: overlapping windows make the
            # whole group one connected dispute component.
            start = (index * 7) % bags
            picked = []
            seen = set()
            offset = 0
            while len(picked) < min(size, bags):
                candidate = bag_ids[(start + offset) % bags]
                offset += 1
                if candidate in seen:
                    continue
                seen.add(candidate)
                picked.append(candidate)
        else:
            picked = rng.sample(bag_ids, min(size, bags))
        picked.sort()
        minute = 30 + (index % 25)
        second = index % 60
        read_rows.append(
            {
                "read_id": f"R{index:05d}",
                "captured_at": f"2026-03-01T10:{minute:02d}:{second:02d}+00:00",
                "candidate_bag_ids": picked,
            }
        )

    batch = {
        "station": STATION,
        "departure_from": "2026-03-01T10:00:00+00:00",
        "departure_until": "2026-03-01T23:59:59+00:00",
        "capture_from": "2026-03-01T10:00:00+00:00",
        "capture_until": "2026-03-01T11:00:00+00:00",
        "reads": read_rows,
    }
    initial = {"flights": flights, "bags": bag_rows, "scans": []}

    # A tiny valid decision set: confirm one read to one of its candidates and
    # discard another, exercising both decision paths at scale.
    confirm_read = read_rows[0]
    decisions = {
        "confirm": [
            {"read_id": confirm_read["read_id"],
             "bag_id": confirm_read["candidate_bag_ids"][0],
             "proof_ref": "IMG-0001"}
        ],
        "discard": [
            {"read_id": read_rows[1]["read_id"], "proof_ref": "IMG-0002"}
        ],
    }
    return initial, batch, decisions


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--bags", type=int, default=4500)
    parser.add_argument("--reads", type=int, default=4500)
    parser.add_argument("--candidates", type=int, default=36000)
    parser.add_argument("--component-share", type=float, default=0.9)
    parser.add_argument("--seed", type=int, default=20260301)
    args = parser.parse_args(argv)

    initial, batch, decisions = build(
        args.bags, args.reads, args.candidates, args.component_share, args.seed
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "initial.json").write_text(json.dumps(initial), encoding="utf-8")
    (out / "batch.json").write_text(json.dumps(batch), encoding="utf-8")
    (out / "decisions.json").write_text(json.dumps(decisions), encoding="utf-8")
    print(json.dumps({
        "out": str(out),
        "bags": args.bags,
        "reads": args.reads,
        "candidate_refs": sum(len(r["candidate_bag_ids"]) for r in batch["reads"]),
    }, sort_keys=True))


if __name__ == "__main__":
    main()

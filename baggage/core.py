from datetime import datetime, timedelta


def _time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timestamps must include a UTC offset")
    return result


def reconcile(data):
    flights = {x["id"]: x for x in data["flights"]}
    bags = {x["id"]: x for x in data["bags"]}
    if len(flights) != len(data["flights"]) or len(bags) != len(data["bags"]):
        raise ValueError("flight and bag IDs must be unique")
    scans = data["scans"]
    if len({x["id"] for x in scans}) != len(scans):
        raise ValueError("scan IDs must be unique")
    for bag in bags.values():
        if bag["flight_id"] not in flights:
            raise ValueError(f"unknown flight for bag {bag['id']}")
        if bag.get("next_flight_id") and bag["next_flight_id"] not in flights:
            raise ValueError(f"unknown next flight for bag {bag['id']}")
    by_bag = {key: [] for key in bags}
    for scan in scans:
        if scan["bag_id"] not in bags:
            raise ValueError(f"scan references unknown bag {scan['bag_id']}")
        by_bag[scan["bag_id"]].append(scan)
    rows = []
    for bag_id, bag in sorted(bags.items()):
        events = sorted(by_bag[bag_id], key=lambda x: (_time(x["event_time"]), x["id"]))
        last = events[-1] if events else None
        status = "not_seen" if not last else ("arrived" if last["station"] == flights[bag["flight_id"]]["destination"] else "in_transit")
        at_risk = False
        if last and bag.get("next_flight_id") and last["station"] == flights[bag["next_flight_id"]]["origin"]:
            limit = _time(last["event_time"]) + timedelta(minutes=int(bag["minimum_connection_minutes"]))
            at_risk = limit > _time(flights[bag["next_flight_id"]]["departure"])
        rows.append({"bag_id": bag_id, "flight_id": bag["flight_id"], "status": status,
                     "last_station": last["station"] if last else None,
                     "last_scan": last["id"] if last else None, "connection_at_risk": at_risk})
    return {"bags": rows, "summary": {"total": len(rows), "not_seen": sum(x["status"] == "not_seen" for x in rows),
             "in_transit": sum(x["status"] == "in_transit" for x in rows),
             "arrived": sum(x["status"] == "arrived" for x in rows),
             "connection_risk": sum(x["connection_at_risk"] for x in rows)}}

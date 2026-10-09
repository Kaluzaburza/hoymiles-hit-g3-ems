"""Read-only qualification of a bounded HA history CSV before Pstryk replay.

No interpolation, inferred prices, solver run, or live acceptance. History CSV
can mix statistics and raw history and does not contain measurement provenance.
Numeric spacing is reported separately from explicit unavailable states.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

MAX_BYTES = 50 * 1024 * 1024
MAX_ROWS = 500_000
UTC = timezone.utc
ZONE = ZoneInfo("Europe/Warsaw")


def stamp(raw):
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp_without_timezone")
    return value.astimezone(UTC)


def inspect(path: Path, *, start: datetime, end: datetime) -> dict:
    start, end = stamp(start.isoformat()), stamp(end.isoformat())
    if start >= end:
        raise ValueError("window_invalid")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("file_size_limit")
    digest = hashlib.sha256()
    with path.open("rb") as raw:
        for chunk in iter(lambda: raw.read(65536), b""):
            digest.update(chunk)
    entities, previous = {}, {}
    total = 0
    csv.field_size_limit(65536)
    with path.open(encoding="utf-8-sig", newline="") as raw:
        reader = csv.DictReader(raw)
        if reader.fieldnames != ["entity_id", "state", "last_changed"]:
            raise ValueError("csv_schema_invalid")
        for row in reader:
            total += 1
            if total > MAX_ROWS:
                raise ValueError("row_limit")
            entity = row["entity_id"]
            if not entity or len(entity) > 255:
                raise ValueError("entity_invalid")
            at = stamp(row["last_changed"])
            stats = entities.setdefault(entity, {
                "rows": 0, "outside_requested_window": 0, "numeric_rows": 0,
                "nonnumeric_states": Counter(), "first_utc": None, "last_utc": None,
                "minimum": None, "maximum": None, "rows_by_local_day": Counter(),
                "max_numeric_spacing_seconds": 0, "numeric_spacings_over_600s": 0,
                "decreases": 0, "duplicate_timestamps": 0, "out_of_order": 0,
                "first_decrease_examples": [],
            })
            stats["rows"] += 1
            if not start <= at <= end:
                stats["outside_requested_window"] += 1
                continue
            stats["rows_by_local_day"][at.astimezone(ZONE).date().isoformat()] += 1
            iso = at.isoformat()
            stats["first_utc"] = min(stats["first_utc"], iso) if stats["first_utc"] else iso
            stats["last_utc"] = max(stats["last_utc"], iso) if stats["last_utc"] else iso
            try:
                value = float(row["state"])
                if not math.isfinite(value):
                    raise ValueError("nonfinite")
            except (TypeError, ValueError):
                # Do not retain arbitrary long state strings in the report.
                state = row["state"] if row["state"] in {"unknown", "unavailable", ""} else "invalid"
                stats["nonnumeric_states"][state] += 1
                previous.pop(entity, None)
                continue
            stats["numeric_rows"] += 1
            stats["minimum"] = min(value, stats["minimum"]) if stats["minimum"] is not None else value
            stats["maximum"] = max(value, stats["maximum"]) if stats["maximum"] is not None else value
            if entity in previous:
                before, old = previous[entity]
                dt = (at - before).total_seconds()
                if dt < 0:
                    stats["out_of_order"] += 1
                elif dt == 0:
                    stats["duplicate_timestamps"] += 1
                else:
                    stats["max_numeric_spacing_seconds"] = max(stats["max_numeric_spacing_seconds"], dt)
                    stats["numeric_spacings_over_600s"] += int(dt > 600)
                if value < old:
                    stats["decreases"] += 1
                    if len(stats["first_decrease_examples"]) < 4:
                        stats["first_decrease_examples"].append({"at": iso, "before": old, "after": value})
            previous[entity] = (at, value)
    return {
        "source_file": path.name, "sha256": digest.hexdigest(), "bytes": path.stat().st_size,
        "requested_start_utc": start.isoformat(), "requested_end_utc": end.isoformat(),
        "rows": total, "entities": entities,
        "qualification": "INSPECTED_NOT_REPLAY_ACCEPTED",
        "limits": ["CSV does not prove units, raw-vs-statistics origin or freshness",
                   "Numeric spacing is not an explicit outage",
                   "Counter decrease needs reset/source validation; SOC decreases are normal",
                   "No prices, forecast availability, physical limits or joint-planner result inferred"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--start", required=True, type=stamp)
    parser.add_argument("--end", required=True, type=stamp)
    args = parser.parse_args()
    print(json.dumps(inspect(args.csv, start=args.start, end=args.end), indent=2))

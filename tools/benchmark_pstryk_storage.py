"""Synthetic callback/cache size benchmark, NOT actual Recorder DB savings."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from time import perf_counter

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "hoymiles_hit_modbus"))
from pstryk_prices import PriceCache, parse_prices, price_projection


def benchmark(hours=72, callbacks_per_hour=60):
    start = datetime(2026, 9, 29, tzinfo=timezone.utc)
    cache = PriceCache("synthetic-benchmark")
    cache_writes = naive_bytes = max_cache = max_projection = 0
    unique, previous = set(), None
    publications = 0
    begin = perf_counter()
    for hour in range(hours):
        at = start + timedelta(hours=hour)
        rows = [{"start": (at + timedelta(hours=i)).isoformat(),
                 "end": (at + timedelta(hours=i + 1)).isoformat(),
                 "priceNet": "0.41234", "priceGross": "0.60518"}
                for i in range(49)]
        for tick in range(callbacks_per_hour):
            now = at + timedelta(seconds=3600 * tick / callbacks_per_hour)
            value = parse_prices({"frames": rows}, source_scope=cache.source_scope,
                                 fetched_at=now, window_start=at, window_end=at + timedelta(hours=49))
            cache.accept(value, now=now)
            record = cache.storage_candidate()
            if record is not None:
                cache_writes += 1
                max_cache = max(max_cache, len(record.payload_json.encode()))
                cache.ack_storage(record)
            projected = json.dumps(price_projection(cache.view(now=now), now=now), sort_keys=True)
            if projected != previous:
                publications += 1
                previous = projected
            unique.add(projected)
            max_projection = max(max_projection, len(projected.encode()))
            naive_bytes += len(json.dumps({"received_at": now.isoformat(), "frames": rows}).encode())
    result = {"scope": "synthetic_serialization_only_not_HA_DB_WAL",
              "hours": hours, "callbacks": hours * callbacks_per_hour,
              "maximum_frames": 49, "cache_write_candidates": cache_writes,
              "semantic_publications": publications, "unique_price_projections": len(unique),
              "maximum_cache_bytes": max_cache, "maximum_projection_bytes": max_projection,
              "naive_full_response_serialized_bytes": naive_bytes,
              "distinct_projection_serialized_bytes": sum(len(x.encode()) for x in unique),
              "runtime_seconds": round(perf_counter() - begin, 4)}
    assert cache_writes == hours and publications == hours and len(unique) == hours
    assert max_cache <= 16384 and max_projection < 1024
    return result


if __name__ == "__main__":
    print(json.dumps(benchmark(), indent=2))

"""Date-valid cache regressions, including the real optimizer row selector."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import test_rce_optimizer as fixtures
from rce_price_cache import RCEPriceCache, WARSAW, cached_state_valid, payload_hash, public_entry
import json

NOW = datetime(2026, 9, 30, 18, tzinfo=WARSAW)
DAY = NOW.date()


def rows_for(day=DAY):
    return fixtures._official_pse_rows_for_local_day(datetime.combine(day, datetime.min.time(), WARSAW))


def test_complete_cache_restart_and_actual_age():
    cache = RCEPriceCache()
    fetched = NOW - timedelta(hours=4)
    cache.accept(DAY, rows_for(), fetched)
    saved = cache.dump()
    restarted = RCEPriceCache()
    restarted.load(saved, NOW)
    assert restarted.dump() == saved
    state = SimpleNamespace(state="96", attributes=restarted.get(DAY, NOW), last_updated=NOW)
    selector, read_rows = fixtures._load_current_rce_price_row_selector()
    rows, age = read_rows(state, NOW)
    assert age == 14400 and cached_state_valid(state, DAY, NOW)
    selected = selector(primary_rows=rows, primary_age_seconds=age, rollover_rows=[],
                        rollover_age_seconds=None, target_date=DAY, timezone=WARSAW,
                        primary_daily_valid=cached_state_valid(state, DAY, NOW))
    assert selected[1] and selected[4], selected
    # Legacy REST freshness is deliberately unchanged.
    legacy = SimpleNamespace(state="96", attributes={"value": rows}, last_updated=fetched)
    raw, old_age = read_rows(legacy, NOW)
    assert not selector(primary_rows=raw, primary_age_seconds=old_age, rollover_rows=[],
                        rollover_age_seconds=None, target_date=DAY, timezone=WARSAW)[4]
    # Unknown and corrupted native sources must not fall back to fresh HA time.
    for bad in ("unknown", "unavailable", "95"):
        state.state = bad
        assert read_rows(state, NOW) == ([], None)
    state.state = "96"
    state.attributes["payload_sha256"] = "0" * 64
    assert read_rows(state, NOW) == ([], None)


def test_rotation_offline_and_dst():
    cache = RCEPriceCache()
    cache.accept(DAY, rows_for(), NOW)
    tomorrow = DAY + timedelta(days=1)
    cache.accept(tomorrow, rows_for(tomorrow), NOW)
    saved = cache.dump()
    midnight = NOW.replace(hour=0) + timedelta(days=1)
    assert cache.get(DAY, midnight) is None
    assert cache.prune(midnight)  # a read must not conceal the required disk prune
    assert set(cache.days) == {tomorrow.isoformat()}
    assert cache.get(tomorrow, midnight)["fetched_at"] == saved["days"][tomorrow.isoformat()]["fetched_at"]
    cache.load(saved, NOW + timedelta(days=4))
    assert cache.days == {}
    for moment, count in ((datetime(2026, 3, 29, 10, tzinfo=WARSAW), 92),
                          (datetime(2026, 10, 25, 10, tzinfo=WARSAW), 100)):
        cache.accept(moment.date(), rows_for(moment.date()), moment)
        assert len(cache.get(moment.date(), moment)["value"]) == count


def test_reject_bad_data_and_preserve_previous():
    cache = RCEPriceCache()
    rows = rows_for()
    rows[0]["rce_pln"] = -200.5
    cache.accept(DAY, rows, NOW)
    saved = cache.dump()
    cases = [rows[:-1], rows + [rows[0]], None]
    for field, bad in (("rce_pln", float("nan")), ("rce_pln", float("inf")),
                       ("rce_pln", True), ("business_date", "2026-09-29"),
                       ("dtime_utc", "2026-09-30 12:00:00"),
                       ("period_utc", "25:00 - 25:15"),
                       ("publication_ts_utc", "2026-10-30 01:00:00")):
        bad_rows = deepcopy(rows)
        bad_rows[0][field] = bad
        cases.append(bad_rows)
    for case in cases:
        try:
            cache.accept(DAY, case, NOW)
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError("invalid payload accepted")
        assert cache.dump() == saved
    for change in (lambda p: p.update(schema_version=99),
                   lambda p: p["days"][DAY.isoformat()].update(payload_sha256="bad"),
                   lambda p: p["days"][DAY.isoformat()].update(fetched_at="2026-09-30T17:00:00")):
        bad = deepcopy(saved)
        change(bad)
        cache.load(bad, NOW)
        assert cache.get(DAY, NOW) is None


def test_corrections_and_provenance():
    cache = RCEPriceCache()
    rows = rows_for()
    for row in rows:
        row["publication_ts_utc"] = "2026-09-29 13:00:00"
    cache.accept(DAY, rows, NOW)
    original = cache.get(DAY, NOW)
    later = NOW + timedelta(minutes=1)
    assert not cache.accept(DAY, rows, later)
    assert cache.get(DAY, later)["fetched_at"] == original["fetched_at"]
    corrected = deepcopy(rows)
    corrected[0]["rce_pln"] += 10
    corrected[0]["publication_ts_utc"] = "2026-09-30 13:00:00"
    assert cache.accept(DAY, corrected, later)
    saved = cache.dump()
    try:
        cache.accept(DAY, rows, later + timedelta(minutes=1))
    except ValueError as err:
        assert str(err) == "older_publication"
    else:
        raise AssertionError("older publication accepted")
    assert cache.dump() == saved
    assert cache.get(DAY, later)["payload_sha256"] == payload_hash(cache.get(DAY, later)["value"])


def test_public_rows_preserve_prices_without_oversized_attributes():
    now = datetime(2026, 10, 25, 10, tzinfo=WARSAW)
    cache = RCEPriceCache()
    rows = rows_for(now.date())
    for i, row in enumerate(rows):
        row["publication_ts_utc"] = (now - timedelta(hours=1, seconds=i)).isoformat()
        row["publication_ts"] = row["publication_ts_utc"]
    cache.accept(now.date(), rows, now)
    entry = cache.get(now.date(), now)
    public = public_entry(entry)
    assert len(json.dumps(public, separators=(",", ":")).encode()) < 16384
    assert public["publication_version_count"] == 100
    assert public["cache_payload_sha256"] == entry["payload_sha256"]
    assert [r["rce_pln"] for r in public["value"]] == [r["rce_pln"] for r in entry["value"]]
    assert cached_state_valid(SimpleNamespace(state="100", attributes=public), now.date(), now)
    assert "publication_ts_utc" in cache.dump()["days"][now.date().isoformat()]["value"][0]


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
            print("PASS", name)

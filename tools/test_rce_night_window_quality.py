"""Night energy qualification uses the requested window and phase episodes."""
from datetime import date, datetime, timedelta
from copy import deepcopy
from test_rce_history import HISTORY as H, complete_dense_day, WARSAW


def main():
    day = date(2026, 9, 24)
    data = complete_dense_day(day)
    for key, rows in complete_dense_day(day + timedelta(days=1)).items():
        data[key] += rows
    start = datetime(2026, 9, 24, 18, tzinfo=WARSAW)
    end = datetime(2026, 9, 25, 8, tzinfo=WARSAW)
    key = H.LOAD_PHASE_ENERGY_ENTITIES[0]
    def summarize(rows):
        return H.summarize_load_history(rows, now=end+timedelta(hours=4),
                                       night_windows={day: (start, end)})
    expected = summarize(data).night_energy_kwh
    assert expected
    for label, hour, duration, accepted in [
        ('outside night short episode', 12, 5, True),
        ('inside night short episode', 20, 5, True),
        ('inside night long episode', 20, 901, False),
    ]:
        changed = deepcopy(data)
        right = start.replace(hour=hour, minute=15)
        left = right-timedelta(minutes=30)
        changed[key] = [(t,v) for t,v in changed[key] if not left<t<right]
        changed[key].append((right-timedelta(seconds=duration), 'unavailable'))
        got = summarize(changed)
        assert bool(got.night_energy_kwh) is accepted, (label, got.night_quality_by_date)
        if accepted:
            assert got.night_energy_kwh == expected
        print('PASS', label)
    for label, stamp, value in [
        ('nonfinite inside night', start+timedelta(hours=1), float('nan')),
        ('negative inside night', start+timedelta(hours=1), -1),
        ('unrecovered availability', end-timedelta(seconds=1), 'unavailable'),
        ('invalid timestamp', 'bad', 1),
    ]:
        changed = deepcopy(data)
        changed[key].append((stamp, value))
        if label == 'unrecovered availability':
            changed[key] = [(t,v) for t,v in changed[key] if t != end]
        assert not summarize(changed).night_energy_kwh, label
        print('PASS denied', label)


if __name__ == '__main__': main()

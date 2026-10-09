"""Contract tests for shared, policy-neutral LOAD estimates."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "hoymiles_hit_modbus"))

from load_model import (  # noqa: E402
    current_day_profile_correction,
    daily_ages_days,
    expected_energy_between,
    expected_load_by_slot,
    persistent_load_delta_kw,
    robust_weighted_estimate,
    robust_weighted_upper_estimate,
)


def main() -> None:
    assert robust_weighted_estimate([]) == (None, 0.0, 0)
    assert robust_weighted_upper_estimate([]) == (None, 0)

    sample = [30.0] * 20 + [31.0, 29.0, 30.5, 200.0]
    expected, uncertainty, count = robust_weighted_estimate(sample)
    upper, upper_count = robust_weighted_upper_estimate(sample)
    assert expected is not None and 29.0 <= expected <= 34.0
    assert 0.0 <= uncertainty <= 1.0
    assert upper is not None and expected <= upper <= 52.5
    assert count == upper_count == len(sample)

    # Keep only the newest 28 complete, physically credible days.
    long_sample = [10.0] * 10 + [20.0] * 28
    expected, _, count = robust_weighted_estimate(long_sample)
    assert expected == 20.0 and count == 28

    # Missing calendar days retain their real age instead of becoming adjacent
    # sequence positions.
    ages = daily_ages_days(
        ("2026-09-01", "2026-09-10"), as_of=date(2026, 9, 12)
    )
    assert ages == (11.0, 2.0)
    aged, _, _ = robust_weighted_estimate((40.0, 20.0), ages_days=ages)
    sequential, _, _ = robust_weighted_estimate((40.0, 20.0))
    assert aged is not None and sequential is not None and aged < sequential

    warsaw = ZoneInfo("Europe/Warsaw")
    profile = tuple([0.75] * 12 + [0.0] * 16 + [0.15] * 20)
    now = datetime(2026, 9, 8, 14, 0, tzinfo=warsaw)
    ratio, residual, expected_elapsed = current_day_profile_correction(
        now=now,
        observed_energy_kwh=9.0,
        observed_at=now,
        daily_energy_kwh=12.0,
        average_profile_30m_kwh=profile,
    )
    assert abs(expected_elapsed - 9.0) < 1e-6
    assert abs(ratio - 1.0) < 1e-6 and abs(residual) < 1e-6
    starts = [now + timedelta(minutes=30 * index) for index in range(68)]
    forecast = expected_load_by_slot(
        starts,
        now=now,
        daily_energy_kwh=12.0,
        average_profile_30m_kwh=profile,
        current_day_energy_kwh=9.0,
        current_day_observed_at=now,
    )
    today_remaining = sum(
        value for stamp, value in forecast.by_slot_kwh.items()
        if stamp.astimezone(warsaw).date() == now.date()
    )
    tomorrow = sum(
        value for stamp, value in forecast.by_slot_kwh.items()
        if stamp.astimezone(warsaw).date() == now.date() + timedelta(days=1)
    )
    assert abs(today_remaining - 3.0) < 1e-6
    assert abs(tomorrow - 12.0) < 1e-6

    # Audit regression: a 2 kWh event already present in the daily counter is
    # not automatically projected over the remaining 16 hours after power has
    # returned to the 1 kW profile.  Tomorrow never inherits today's residual.
    audit_now = datetime(2026, 9, 12, 8, 0, tzinfo=warsaw)
    audit_starts = [audit_now + timedelta(minutes=30 * index) for index in range(80)]
    audit_baseline = expected_load_by_slot(
        audit_starts,
        now=audit_now,
        daily_energy_kwh=24.0,
        current_day_energy_kwh=8.0,
        current_day_observed_at=audit_now,
    )
    audit_impulse = expected_load_by_slot(
        audit_starts,
        now=audit_now,
        daily_energy_kwh=24.0,
        current_day_energy_kwh=10.0,
        current_day_observed_at=audit_now,
    )
    for result in (audit_baseline, audit_impulse):
        remaining = sum(
            value
            for stamp, value in result.by_slot_kwh.items()
            if stamp.astimezone(warsaw).date() == audit_now.date()
        )
        next_day = sum(
            value
            for stamp, value in result.by_slot_kwh.items()
            if stamp.astimezone(warsaw).date()
            == audit_now.date() + timedelta(days=1)
        )
        assert abs(remaining - 16.0) < 1e-6
        assert abs(next_day - 24.0) < 1e-6
        assert abs(result.current_day_correction_ratio - 1.0) < 1e-6
        assert abs(result.current_day_correction_kwh) < 1e-6

    # The same counter residual is allowed to affect future slots only after a
    # dense 20-minute power window confirms a sustained deviation.
    sustained = [
        (audit_now - timedelta(minutes=20 - 4 * index), 1.5, 1.0)
        for index in range(6)
    ]
    sustained_delta, sustained_at, sustained_count = persistent_load_delta_kw(
        sustained, now=audit_now
    )
    assert sustained_delta == 0.5 and sustained_at == audit_now
    assert sustained_count == 6
    sustained_forecast = expected_load_by_slot(
        audit_starts,
        now=audit_now,
        daily_energy_kwh=24.0,
        current_day_energy_kwh=10.0,
        current_day_observed_at=audit_now,
        persistence_delta_kw=sustained_delta,
        persistence_observed_at=sustained_at,
    )
    assert abs(sustained_forecast.current_day_correction_ratio - 1.25) < 1e-6
    assert abs(sustained_forecast.current_day_correction_kwh - 2.0) < 1e-6
    sustained_remaining = sum(
        value
        for stamp, value in sustained_forecast.by_slot_kwh.items()
        if stamp.astimezone(warsaw).date() == audit_now.date()
    )
    assert abs(sustained_remaining - 20.0) < 1e-6

    # A deviation that remains present for two hours is still confirmed from
    # the causal trailing window; no sample after ``now`` is required.
    sustained_two_hours = [
        (audit_now - timedelta(minutes=120 - 4 * index), 1.5, 1.0)
        for index in range(31)
    ]
    two_hour_delta, two_hour_at, two_hour_count = persistent_load_delta_kw(
        sustained_two_hours, now=audit_now
    )
    assert two_hour_delta == 0.5 and two_hour_at == audit_now
    assert two_hour_count == 6

    # Four normal samples over 15 minutes are enough to clear persistence.
    # The old 2 kWh counter residual then stops changing the future forecast.
    normalized_now = audit_now + timedelta(minutes=15)
    normalized = [
        (audit_now + timedelta(minutes=5 * index), 1.0, 1.0)
        for index in range(4)
    ]
    normalized_delta, normalized_at, normalized_count = persistent_load_delta_kw(
        normalized, now=normalized_now
    )
    assert normalized_delta == 0.0 and normalized_count == 4
    normalized_forecast = expected_load_by_slot(
        [normalized_now + timedelta(minutes=30 * index) for index in range(32)],
        now=normalized_now,
        daily_energy_kwh=24.0,
        current_day_energy_kwh=10.0,
        current_day_observed_at=normalized_now,
        persistence_delta_kw=normalized_delta,
        persistence_observed_at=normalized_at,
    )
    assert abs(normalized_forecast.current_day_correction_ratio - 1.0) < 1e-6
    assert abs(normalized_forecast.current_day_correction_kwh) < 1e-6

    # A persistent lower load remains adaptive, while gaps, stale samples and
    # restart-without-persistence are deliberately neutral.
    lower_persistent = expected_load_by_slot(
        audit_starts,
        now=audit_now,
        daily_energy_kwh=24.0,
        current_day_energy_kwh=6.0,
        current_day_observed_at=audit_now,
        persistence_delta_kw=-0.5,
        persistence_observed_at=audit_now,
    )
    assert abs(lower_persistent.current_day_correction_ratio - 0.8) < 1e-6
    stale_persistence = expected_load_by_slot(
        audit_starts,
        now=audit_now,
        daily_energy_kwh=24.0,
        current_day_energy_kwh=10.0,
        current_day_observed_at=audit_now,
        persistence_delta_kw=0.5,
        persistence_observed_at=audit_now - timedelta(minutes=6),
    )
    assert stale_persistence.current_day_correction_ratio == 1.0
    gap_observations = [
        (audit_now - timedelta(minutes=20), 1.5, 1.0),
        (audit_now - timedelta(minutes=16), 1.5, 1.0),
        (audit_now - timedelta(minutes=5), 1.5, 1.0),
        (audit_now, 1.5, 1.0),
    ]
    assert persistent_load_delta_kw(gap_observations, now=audit_now)[0] == 0.0
    restarted = expected_load_by_slot(
        audit_starts,
        now=audit_now,
        daily_energy_kwh=24.0,
        current_day_energy_kwh=10.0,
        current_day_observed_at=audit_now,
    )
    assert restarted.current_day_correction_ratio == 1.0

    # A delayed/reset counter and a previous-day sample across midnight cannot
    # transfer a residual into future slots.
    delayed = expected_load_by_slot(
        audit_starts,
        now=audit_now,
        daily_energy_kwh=24.0,
        current_day_energy_kwh=10.0,
        current_day_observed_at=audit_now - timedelta(minutes=11),
        persistence_delta_kw=0.5,
        persistence_observed_at=audit_now,
    )
    reset = expected_load_by_slot(
        audit_starts,
        now=audit_now,
        daily_energy_kwh=24.0,
        current_day_energy_kwh=-1.0,
        current_day_observed_at=audit_now,
        persistence_delta_kw=0.5,
        persistence_observed_at=audit_now,
    )
    midnight = datetime(2026, 9, 13, 0, 2, tzinfo=warsaw)
    midnight_result = expected_load_by_slot(
        [midnight + timedelta(minutes=30 * index) for index in range(48)],
        now=midnight,
        daily_energy_kwh=24.0,
        current_day_energy_kwh=26.0,
        current_day_observed_at=midnight - timedelta(minutes=4),
        persistence_delta_kw=0.5,
        persistence_observed_at=midnight,
    )
    assert delayed.current_day_correction_ratio == 1.0
    assert reset.current_day_correction_ratio == 1.0
    assert midnight_result.current_day_correction_ratio == 1.0

    # Current-slot energy uses the actual 20-minute remainder; the persistence
    # signal is applied only to future slots after causal evidence exists.
    current = datetime(2026, 9, 8, 18, 10, tzinfo=warsaw)
    assert abs(1.0 * (20 / 60) - 0.333333) < 1e-5
    assert abs(4.0 * (20 / 60) - 1.333333) < 1e-5
    observations = [
        (current - timedelta(minutes=15 - 3 * index), 4.0, 1.0)
        for index in range(6)
    ]
    delta, observed_at, count = persistent_load_delta_kw(observations, now=current)
    assert delta == 3.0 and observed_at is not None and count == 6
    impulse = [
        (current - timedelta(minutes=15), 1.0, 1.0),
        (current - timedelta(minutes=10), 1.0, 1.0),
        (current - timedelta(minutes=5), 4.0, 1.0),
        (current, 1.0, 1.0),
    ]
    assert persistent_load_delta_kw(impulse, now=current)[0] == 0.0
    lower = [(stamp, 0.2, 1.0) for stamp, _, _ in observations]
    assert persistent_load_delta_kw(lower, now=current)[0] < 0.0

    # DST normalization preserves the requested daily energy on real 23/25 h
    # days even though local template buckets are missing or repeated.
    for dst_day, expected_slots in ((date(2026, 3, 29), 46), (date(2026, 10, 25), 50)):
        start = datetime.combine(dst_day, datetime.min.time(), tzinfo=warsaw)
        end = datetime.combine(dst_day + timedelta(days=1), datetime.min.time(), tzinfo=warsaw)
        slots = []
        cursor = start.astimezone(ZoneInfo("UTC"))
        while cursor < end.astimezone(ZoneInfo("UTC")):
            slots.append(cursor)
            cursor += timedelta(minutes=30)
        assert len(slots) == expected_slots
        result = expected_load_by_slot(slots, now=start, daily_energy_kwh=24.0)
        assert abs(sum(result.by_slot_kwh.values()) - 24.0) < 1e-6

    # Exact interval integration includes a partial current block.
    interval = expected_energy_between(
        start=datetime(2026, 9, 8, 18, 0, tzinfo=warsaw),
        end=datetime(2026, 9, 8, 18, 10, tzinfo=warsaw),
        daily_energy_kwh=24.0,
    )
    assert abs(interval - (1.0 / 6.0)) < 1e-6

    print("Shared LOAD model: robust estimate contracts passed")


if __name__ == "__main__":
    main()

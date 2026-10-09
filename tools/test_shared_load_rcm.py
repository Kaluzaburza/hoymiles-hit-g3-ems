"""RCEm consumes the same UTC LOAD model, including partial slots and DST."""
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from custom_components.hoymiles_hit_modbus.load_model import expected_load_by_slot
from custom_components.hoymiles_hit_modbus.rcm_optimizer import shared_rcm_load_envelopes
from custom_components.hoymiles_hit_modbus import rcm_sensor as sensor


def main():
    zone = ZoneInfo('Europe/Warsaw')
    for day, slots in [('2026-10-06', 48), ('2026-03-29', 46), ('2026-10-25', 50)]:
        now = datetime.fromisoformat(day + 'T00:07:30').replace(tzinfo=zone)
        for profile in [(), (.2,)*20 + (.7,)*28]:
            model = dict(daily_energy_kwh=24., average_profile_30m_kwh=profile,
                         night_energy_kwh=6., night_start_minute=1200, night_end_minute=420)
            result = shared_rcm_load_envelopes(now=now, target_date=now.date(), **model)
            starts = tuple(result.nominal.by_start_kwh)
            common = expected_load_by_slot(starts, now=now, **model)
            assert len(starts) == slots
            assert result.nominal.by_start_kwh == common.by_slot_kwh
            assert abs(sum(common.by_slot_kwh.values()) - 24.) < 1e-8
            assert all(result.low.by_start_kwh[t] <= value <= result.high.by_start_kwh[t]
                       for t, value in common.by_slot_kwh.items())
            timeline = sensor._timeline_energy_points(now=now, target_date=now.date(),
                risk_day_offset=0, pv_profile=(0.,)*48, load_profile=result.nominal.slot_kwh,
                load_by_start_kwh=dict(common.by_slot_kwh))
            # No energy is invented for elapsed time, the spring gap or a fold.
            remaining = sum(value * max(0., min(1.,
                ((stamp+timedelta(minutes=30))-now.astimezone(timezone.utc)).total_seconds()/1800.))
                for stamp, value in common.by_slot_kwh.items())
            assert abs(sum(p.load_kwh for p in timeline)-remaining) < 1e-8

    # Execute the real HA sensor's risk integration, with only its external
    # inputs stubbed. Historical shape sums to 9.6, common daily total is 24.
    now = datetime(2026, 10, 6, 12, 7, 30, tzinfo=zone)
    load = SimpleNamespace(ready=True, generated_at=now, average_daily_home_load_kwh=24.,
        average_night_home_load_kwh=6., daily_totals_kwh=(24.,)*4,
        average_profile_30m_kwh=(.2,)*48, weekday_profile_30m_kwh=(), weekend_profile_30m_kwh=(),
        current_day_energy_kwh=None, current_day_observed_at=None,
        persistence_delta_kw=0., persistence_observed_at=None,
        night_start_minute=1200, night_end_minute=420)
    obj = object.__new__(sensor.HoymilesRCMOptimizerSensor)
    obj.hass = SimpleNamespace(config=SimpleNamespace(time_zone='Europe/Warsaw'))
    obj._runtime = SimpleNamespace()
    obj._history = SimpleNamespace(risk_windows=((720,780,252.),))
    with patch.object(sensor.dt_util, 'now', return_value=now), \
         patch.object(sensor, '_shared_inputs_snapshot', return_value=SimpleNamespace(load=load, captured_at=now)), \
         patch.object(sensor, '_resolved_forecast_entity_id', return_value='sensor.fixture'), \
         patch.object(sensor, '_first_numeric_state', return_value=('sensor.fixture', SimpleNamespace(attributes={}))), \
         patch.object(sensor, '_forecast_total', return_value=0.), \
         patch.object(sensor, '_detailed_pv_expected_elapsed_kwh', return_value=0.), \
         patch.object(sensor, '_detailed_pv_map', return_value={}):
        actual = obj._expected_risk_surplus_kwh(10., 5., .95, .95, 20., 80., 20.)
    # Daily quantile equals nominal. 52.5 minutes at 1 kW, including seconds.
    assert abs(actual.expected_load_kwh - .875) < 1e-8, actual
    assert actual.load_profile_source.startswith('shared_')
    assert abs(sum(p.load_kwh for p in actual.timeline_energy_points) - 11.875) < 1e-8
    print('PASS RCEm common nominal, margins, real sensor partial seconds, 23/24/25-hour days')


if __name__ == '__main__':
    main()

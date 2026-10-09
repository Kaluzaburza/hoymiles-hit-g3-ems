"""Field regressions for atomic PV/BAT/GRID/LOAD tariff evidence."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import test_supervisor_sensor_contract as h


FIELD_SEQUENCES = (
    (0.000, 0.119, 0.180, 0.259),
    (0.000, 0.114, 0.177, 0.261),
    (0.000, 0.113, 0.176, 0.259),
)


def main() -> None:
    for case, offsets in enumerate(FIELD_SEQUENCES, start=1):
        hass, entry, _runtime, sensor = h.environment()
        h.add(sensor)
        sensor._controller = SimpleNamespace(
            record=SimpleNamespace(
                transaction=SimpleNamespace(owner=h.SENSOR.ExecutionOwner.TARIFF)
            )
        )
        recomputes = 0

        def recompute() -> None:
            nonlocal recomputes
            recomputes += 1

        sensor._recompute = recompute

        def entity_id(key: str) -> str:
            return h._source_entity_id(h.SENSOR._SOURCE_BY_KEY[key], entry.entry_id)

        start = h.NOW + timedelta(minutes=case)
        values = {
            "pv_power": 3100,
            "battery_power": -1800,
            "grid_power": -2500,
            "load_power": 1200,
        }
        for (key, value), offset in zip(values.items(), offsets, strict=True):
            at = start + timedelta(seconds=offset)
            hass.fire_report(entity_id(key), h.FakeState(str(value), reported=at))
            if key != "load_power":
                frozen = sensor._read_source_states()
                assert float(frozen[key].state) != value
                assert recomputes == (1 if key == "grid_power" else 0)

        assert recomputes == 2
        assert sensor._power_cohort_generation == 2
        accepted = sensor._read_source_states()
        assert {key: float(accepted[key].state) for key in values} == values
        assert not hass.active_delays()

        previous = dict(sensor._power_cohort_states)
        for key in ("pv_power", "battery_power", "grid_power"):
            reported = start + timedelta(seconds=1)
            value = 1000 if key == "grid_power" else values[key] + 100
            hass.fire_report(
                entity_id(key),
                h.FakeState(str(value), reported=reported),
            )
        frozen = sensor._read_source_states()
        snapshots = sensor._build_snapshots(frozen, reported)
        execution = snapshots[-1]
        assert execution.grid_power_w == values["grid_power"]
        assert execution.critical_grid_power_w == 1000
        assert execution.critical_grid_power_observed_at == reported
        pending = hass.active_delays()
        assert len(pending) == 1
        assert pending[0].when == h.SENSOR._POWER_COHORT_COLLECTION_SECONDS
        pending[0].run()
        assert sensor._power_cohort_generation == 2
        assert sensor._power_cohort_states == previous
        assert recomputes == 3

    print(
        "Tariff power cohort field regressions: PASS "
        "(3 complete + 3 partial + independent critical GRID)"
    )


if __name__ == "__main__":
    main()

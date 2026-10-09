"""Production RCE listener and publication fingerprint ignore display LOAD."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import test_supervisor_sensor_contract as stub


def production_rce_module():
    core = sys.modules["homeassistant.core"]
    core.EventStateChangedData = dict
    sys.modules["homeassistant.util.dt"].now = lambda: datetime(
        2026, 9, 25, 12, tzinfo=timezone.utc
    )
    helpers = sys.modules["homeassistant.helpers"]
    helpers.sun = stub._module(
        "homeassistant.helpers.sun",
        get_astral_event_date=lambda *_args, **_kwargs: None,
    )
    helpers.storage = stub._module(
        "homeassistant.helpers.storage",
        Store=type("Store", (), {}),
    )
    stub._module(
        "custom_components.hoymiles_hit_modbus.bounded_history",
        RecorderHistoryLimitExceeded=type("RecorderHistoryLimitExceeded", (Exception,), {}),
        RecorderHistoryQueryTimeout=type("RecorderHistoryQueryTimeout", (Exception,), {}),
        async_get_bounded_state_reports=lambda *_args, **_kwargs: None,
    )
    sys.modules.pop("custom_components.hoymiles_hit_modbus.rce_sensor", None)
    return stub._load(
        "custom_components.hoymiles_hit_modbus.rce_sensor",
        stub.COMPONENT / "rce_sensor.py",
    )


def main() -> None:
    module = production_rce_module()
    timestamp = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
    scheduled = []
    published = []
    states = {}

    def state(value, energy, coverage):
        return SimpleNamespace(
            state=value,
            attributes={
                "statistics_energy_kwh": energy,
                "statistics_coverage_days": coverage,
            },
            last_reported=timestamp,
            last_updated=timestamp,
        )

    class States:
        def get(self, entity_id):
            return states.get(entity_id)

    probe = object.__new__(module.HoymilesRCEOptimizerSensor)
    probe.hass = SimpleNamespace(states=States(), config=SimpleNamespace(time_zone='Europe/Warsaw'))
    probe._runtime = None
    probe._tariff_plan_source = None
    probe._tariff_price_source = None
    probe._configured_forecast_source_ids = lambda: frozenset()
    probe._input_revision = module.OptimizerInputRevision()
    probe._attributes = {"result_current": True, "recalculation_pending": False}
    probe._timeline_sensor = None
    probe._stale_result_retry_cancel = None
    probe._recalculate_cancel = None
    probe._lifecycle_stopped = False
    probe._full_plan_trigger = "periodic"
    probe.async_write_ha_state = lambda: published.append(dict(probe._attributes))
    original_schedule = module.async_call_later
    original_gcf_signature = module._live_forecast_gcf_optimizer_signature
    module.async_call_later = lambda _hass, delay, callback: scheduled.append(
        (delay, callback)
    ) or (lambda: None)
    module._live_forecast_gcf_optimizer_signature = lambda _hass, _runtime: (
        False, "disabled", None, None, None, None, None,
    )
    try:
        for entity_id in (
            "sensor.hoymiles_load_average_4_days",
            "sensor.hoymiles_night_load_average_4_days",
        ):
            states[entity_id] = state("15.4", 15.4, 1)
        captured = probe._current_input_fingerprint()
        for entity_id in tuple(states):
            for energy, coverage in ((16.2, 1), (15.4, 2)):
                old = states[entity_id]
                changed = state("15.4", energy, coverage)
                states[entity_id] = changed
                # Dispatch through the production subscription set used by
                # async_added_to_hass, with the real revision/scheduler path.
                if entity_id in module.RCE_EVENT_DRIVEN_ENTITIES:
                    probe._async_input_changed(SimpleNamespace(data={
                        "entity_id": entity_id,
                        "old_state": old,
                        "new_state": changed,
                    }))
        assert probe._input_revision.value == 0, "display diagnostics advanced RCE revision"
        assert not scheduled, "display diagnostics queued immediate solve"
        assert not published, "display diagnostics withdrew current plan"
        assert probe._current_input_fingerprint() == captured, (
            "display diagnostics rejected an in-flight RCE result"
        )
        assert not probe._reject_stale_executor_result(0, captured)
        fallback_id = module.EMS_FALLBACK_DAILY_LOAD_HELPER
        assert fallback_id in module.RCE_EVENT_DRIVEN_ENTITIES
        old_fallback = state("17", 0, 0)
        new_fallback = state("18", 0, 0)
        states[fallback_id] = new_fallback
        probe._async_input_changed(SimpleNamespace(data={
            "entity_id": fallback_id,
            "old_state": old_fallback,
            "new_state": new_fallback,
        }))
        assert probe._input_revision.value == 1
        assert probe._attributes["recalculation_pending"] is True
        assert scheduled and published
        assert probe._current_input_fingerprint() != captured
        assert probe._reject_stale_executor_result(0, captured)
    finally:
        module.async_call_later = original_schedule
        module._live_forecast_gcf_optimizer_signature = original_gcf_signature
    assert "sensor.hoymiles_load_average_4_days" not in module.WATCHED_ENTITIES
    assert "sensor.hoymiles_night_load_average_4_days" not in module.WATCHED_ENTITIES
    print("RCE display LOAD is outside planner inputs: PASS")


if __name__ == "__main__":
    main()

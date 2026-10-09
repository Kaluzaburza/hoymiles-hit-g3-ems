"""Exercise the actual HA adapter; optional base tools path gives a RED replay."""
import sys

if len(sys.argv) > 1:
    sys.path.insert(0, sys.argv[1])
import test_supervisor_sensor_contract as sc


def run():
    hass, entry, runtime, sensor = sc.environment()
    sc.add(sensor)
    for key in ("allow_rce", "rce_enabled", "rce_control_data_ready",
                "rce_price_above_threshold", "rce_reserve_ready", "ems_execution_ready"):
        hass.states.values[sc.SENSOR._SOURCE_BY_KEY[key].locator] = sc.FakeState("on")
    hass.states.values[sc.SENSOR._SOURCE_BY_KEY["supervisor_mode"].locator] = sc.FakeState("Active")
    plan_id = sensor._source_entity_ids["rce_plan"]
    plan = sc._plan_attributes("rce_plan")
    plan.update(current_slot_planned=True, current_slot_start_eligible=True,
                current_slot_continue_eligible=True)
    for power in (0.199, 0.2, 0.201, None, float("nan")):
        plan["current_slot_execution_export_power_kw"] = power
        hass.states.values[plan_id] = sc.FakeState("ready", dict(plan))
        sensor._recompute()
        attrs = sensor.extra_state_attributes
        selected = attrs.get("selected_policy")
        expected = power in (0.2, 0.201)
        assert (selected == "rce") == expected, (power, selected, attrs.get("candidate_summaries"))
        if power == 0.199:
            assert "below_minimum_grid_power" in str(attrs["candidate_summaries"])
    print("Actual HA adapter: 199 W rejected, 200/201 W eligible, missing/NaN fail closed: PASS")
    hass.states.values[sc.SENSOR._SOURCE_BY_KEY["allow_rce"].locator] = sc.FakeState("off")
    for key in ("allow_tariff", "tariff_enabled", "tariff_control_data_ready", "tariff_planned_charge_slot"):
        hass.states.values[sc.SENSOR._SOURCE_BY_KEY[key].locator] = sc.FakeState("on")
    tariff_id = sensor._source_entity_ids["tariff_plan"]
    tariff = sc._plan_attributes("tariff_plan")
    tariff.update(current_slot_planned=True, current_run_start_eligible=True,
                  current_run_continue_eligible=True, current_run_need_class="economic")
    for action in ("battery_charge", "grid_support", "grid_support_and_charge"):
        for power in (0.199, 0.2, 0.6, None):
            tariff.update(current_action=action, requested_charge_power_kw=0.1,
                          current_slot_planned_import_power_kw=power)
            hass.states.values[tariff_id] = sc.FakeState("ready", dict(tariff))
            sensor._recompute()
            attrs = sensor.extra_state_attributes
            assert (attrs.get("selected_policy") == "tariff") == (power in (0.2, 0.6)), (action, power, attrs["candidate_summaries"])
    print("Actual HA adapter: all tariff actions use total planned import, not charge ceiling: PASS")


if __name__ == "__main__":
    run()

"""Deterministic A0 contracts for the always-on baseline energy forecast."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import copysign
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
sys.path.insert(0, str(COMPONENT))

import baseline_energy_timeline as BASE  # noqa: E402


NOW = datetime(2026, 9, 1, 10, 10, tzinfo=timezone.utc)


def inputs(**changes: Any) -> BASE.BaselineEnergyInputs:
    values: dict[str, Any] = {
        "generated_at": NOW,
        "config_entry_id": "entry-a0",
        "shared_inputs_revision": 7,
        "current_soc_percent": 55.0,
        "battery_capacity_kwh": 10.0,
        "reserve_soc_percent": 20.0,
        "hardware_minimum_soc_percent": 10.0,
        "hardware_maximum_soc_percent": 100.0,
        "pv_to_battery_efficiency": 0.95,
        "battery_to_home_efficiency": 0.9,
        "maximum_charge_power_kw": 5.0,
        "maximum_discharge_power_kw": 5.0,
        "system_ac_power_kw": 10.0,
        "zero_export_confirmed": False,
        "export_allowed": True,
        "sources": {
            "soc": "sensor.physical_soc",
            "capacity": "sensor.physical_capacity",
        },
        "provenance": {"soc": "physical_fc03"},
    }
    values.update(changes)
    return BASE.BaselineEnergyInputs(**values)


def slots(
    values: tuple[tuple[float | None, float | None], ...] = (
        (2.0, 1.0),
        (0.0, 2.0),
    ),
) -> list[BASE.BaselineForecastSlot]:
    result: list[BASE.BaselineForecastSlot] = []
    start = NOW
    for pv_kw, load_kw in values:
        end = start + timedelta(minutes=30)
        result.append(
            BASE.BaselineForecastSlot(
                start=start,
                end=end,
                pv_kw=pv_kw,
                load_kw=load_kw,
                pv_source="sensor.pv_forecast",
                load_source="recorder_load_model",
                pv_provenance="detailed_forecast_p50",
                load_provenance="average_profile_30m_kwh",
            )
        )
        start = end
    return result


def build(
    *,
    input_changes: dict[str, Any] | None = None,
    slot_values: tuple[tuple[float | None, float | None], ...] | None = None,
) -> dict[str, Any]:
    shared = inputs(**(input_changes or {}))
    forecast_slots = slots(slot_values) if slot_values is not None else slots()
    return BASE.build_baseline_energy_timeline(shared, forecast_slots)


def test_output_only_authority_contract() -> None:
    payload = build()
    assert BASE.BASELINE_TIMELINE_SCHEMA_VERSION == "2.0"
    assert payload["schema_version"] == BASE.BASELINE_TIMELINE_SCHEMA_VERSION
    assert set(payload) == BASE.BASELINE_PAYLOAD_KEYS
    assert payload["timeline_kind"] == "baseline_self_use"
    assert payload["output_only"] is True
    assert payload["authority"] is False
    assert payload["supervisor_candidate"] is False
    assert payload["pv_to_battery_efficiency"] == 0.95
    assert payload["battery_to_home_efficiency"] == 0.9
    assert payload["maximum_charge_power_kw"] == 5.0
    assert payload["maximum_discharge_power_kw"] == 5.0
    assert payload["system_ac_power_kw"] == 10.0
    serialized = repr(payload).casefold()
    assert "owner" not in serialized
    assert "selected_policy" not in serialized
    assert "action_band" not in serialized


def test_supervisor_and_policy_state_cannot_change_baseline() -> None:
    original = build()
    # The baseline API intentionally accepts no Supervisor/owner/policy state.
    # Therefore Off, Active owner-none, RCEm unavailable and RCE blocked all
    # have one identical deterministic result for identical shared inputs.
    for _irrelevant_state in (
        "supervisor_off",
        "supervisor_active_owner_none",
        "rcem_unavailable",
        "rce_blocked_zero_export",
    ):
        assert build() == original


def test_below_reserve_uses_grid_and_pv_can_rebuild_soc() -> None:
    payload = build(
        input_changes={"current_soc_percent": 15.0, "reserve_soc_percent": 20.0},
        slot_values=((0.0, 2.0), (4.0, 1.0)),
    )
    deficit, surplus = payload["points"]
    assert deficit["battery_kw"] == 0.0
    assert copysign(1.0, deficit["battery_kw"]) == 1.0
    assert deficit["grid_import_kw"] == 2.0
    assert deficit["soc_start_percent"] == 15.0
    assert deficit["soc_end_percent"] == 15.0
    assert surplus["battery_kw"] == 3.0
    assert surplus["grid_import_kw"] == 0.0
    assert surplus["soc_end_percent"] > surplus["soc_start_percent"]


def test_full_battery_confirmed_zero_export_never_fakes_export() -> None:
    payload = build(
        input_changes={
            "current_soc_percent": 100.0,
            "zero_export_confirmed": True,
            "export_allowed": False,
        },
        slot_values=((5.0, 1.0),),
    )
    point = payload["points"][0]
    assert point["battery_kw"] == 0.0
    assert point["grid_export_kw"] == 0.0
    assert point["soc_start_percent"] == 100.0
    assert point["soc_end_percent"] == 100.0


def test_bms_dc_power_limits_are_converted_at_the_efficiency_boundary() -> None:
    charging = build(
        input_changes={
            "battery_capacity_kwh": 100.0,
            "current_soc_percent": 50.0,
            "pv_to_battery_efficiency": 0.8,
            "maximum_charge_power_kw": 4.0,
        },
        slot_values=((20.0, 0.0),),
    )["points"][0]
    # 4 kW is the BMS-side stored-energy rate, hence 5 kW may enter at 80%.
    assert charging["battery_kw"] == 5.0
    assert charging["soc_end_percent"] == 52.0

    discharging = build(
        input_changes={
            "battery_capacity_kwh": 100.0,
            "current_soc_percent": 50.0,
            "battery_to_home_efficiency": 0.8,
            "maximum_discharge_power_kw": 4.0,
        },
        slot_values=((0.0, 20.0),),
    )["points"][0]
    # At most 4 kW leaves the battery; the AC home receives 3.2 kW.
    assert discharging["battery_kw"] == -3.2
    assert discharging["grid_import_kw"] == 16.8
    assert discharging["soc_end_percent"] == 48.0


def test_efficiency_seed_parity_and_directional_independence() -> None:
    """I3 keeps seeded math stable and separates both neutral directions."""

    legacy_values = {
        "pv_to_battery_efficiency": 0.95,
        "battery_to_home_efficiency": 0.94,
    }
    before = build(input_changes=legacy_values)
    seeded_after = build(input_changes=dict(legacy_values))
    assert seeded_after["points"] == before["points"]
    assert seeded_after["state"] == before["state"]

    charge_before = build(
        input_changes=legacy_values,
        slot_values=((5.0, 1.0),),
    )["points"][0]
    charge_changed = build(
        input_changes={**legacy_values, "pv_to_battery_efficiency": 0.8},
        slot_values=((5.0, 1.0),),
    )["points"][0]
    charge_other_direction = build(
        input_changes={**legacy_values, "battery_to_home_efficiency": 0.8},
        slot_values=((5.0, 1.0),),
    )["points"][0]
    assert charge_changed["soc_end_percent"] != charge_before["soc_end_percent"]
    assert charge_other_direction == charge_before

    discharge_before = build(
        input_changes=legacy_values,
        slot_values=((0.0, 4.0),),
    )["points"][0]
    discharge_changed = build(
        input_changes={**legacy_values, "battery_to_home_efficiency": 0.8},
        slot_values=((0.0, 4.0),),
    )["points"][0]
    discharge_other_direction = build(
        input_changes={**legacy_values, "pv_to_battery_efficiency": 0.8},
        slot_values=((0.0, 4.0),),
    )["points"][0]
    assert discharge_changed["soc_end_percent"] != discharge_before["soc_end_percent"]
    assert discharge_other_direction == discharge_before


def test_observed_soc_above_hardware_maximum_never_drops_without_flow() -> None:
    point = build(
        input_changes={
            "current_soc_percent": 95.0,
            "hardware_maximum_soc_percent": 90.0,
        },
        slot_values=((5.0, 1.0),),
    )["points"][0]
    assert point["battery_kw"] == 0.0
    assert point["soc_start_percent"] == 95.0
    assert point["soc_end_percent"] == 95.0


def test_missing_soc_keeps_exact_pv_and_load() -> None:
    payload = build(input_changes={"current_soc_percent": None})
    assert payload["state"] == "partial"
    assert payload["current_actual_soc_percent"] is None
    for point, expected in zip(payload["points"], ((2.0, 1.0), (0.0, 2.0))):
        assert (point["pv_kw"], point["load_kw"]) == expected
        assert point["battery_kw"] is None
        assert point["soc_start_percent"] is None
        assert point["soc_end_percent"] is None


def test_missing_capacity_keeps_exact_pv_and_load() -> None:
    payload = build(input_changes={"battery_capacity_kwh": None})
    assert [(p["pv_kw"], p["load_kw"]) for p in payload["points"]] == [
        (2.0, 1.0),
        (0.0, 2.0),
    ]
    assert all(point["soc_end_percent"] is None for point in payload["points"])


def test_missing_hardware_ceiling_is_explicit_but_keeps_backend_soc() -> None:
    payload = build(input_changes={"hardware_maximum_soc_percent": None})
    assert payload["state"] == "partial"
    assert payload["effective_maximum_soc_percent"] == 100.0
    assert all(
        point["blocker_code"] == "hardware_soc_bounds_partial"
        for point in payload["points"]
    )
    assert all(point["soc_end_percent"] is not None for point in payload["points"])


def test_missing_pv_degrades_only_pv_and_dependent_series() -> None:
    payload = build(slot_values=((None, 1.25), (None, 1.75)))
    assert [point["load_kw"] for point in payload["points"]] == [1.25, 1.75]
    assert all(point["pv_kw"] is None for point in payload["points"])
    assert all(point["soc_end_percent"] is None for point in payload["points"])


def test_missing_load_degrades_only_load_and_dependent_series() -> None:
    payload = build(slot_values=((1.25, None), (1.75, None)))
    assert [point["pv_kw"] for point in payload["points"]] == [1.25, 1.75]
    assert all(point["load_kw"] is None for point in payload["points"])
    assert all(point["soc_end_percent"] is None for point in payload["points"])


def test_unknown_export_permission_is_not_interpreted() -> None:
    payload = build(
        input_changes={
            "current_soc_percent": 100.0,
            "zero_export_confirmed": False,
            "export_allowed": None,
        },
        slot_values=((5.0, 1.0),),
    )
    point = payload["points"][0]
    assert point["grid_export_kw"] is None
    assert point["blocker_code"] == "zero_export_state_unverified"
    assert point["pv_kw"] == 5.0
    assert point["load_kw"] == 1.0


def test_unknown_export_permission_still_proves_zero_when_battery_absorbs_all() -> None:
    payload = build(
        input_changes={
            "current_soc_percent": 50.0,
            "zero_export_confirmed": False,
            "export_allowed": None,
        },
        slot_values=((4.0, 1.0),),
    )
    point = payload["points"][0]
    assert point["battery_kw"] == 3.0
    assert point["grid_export_kw"] == 0.0
    assert point["quality"] == "current"
    assert point["blocker_code"] is None


def test_inspector_points_are_exact_backend_values_without_interpolation() -> None:
    exact = ((0.13, 1.17), (4.91, 0.42), (0.0, 3.33))
    payload = build(slot_values=exact)
    assert payload["point_count"] == 3
    assert [
        (point["pv_kw"], point["load_kw"]) for point in payload["points"]
    ] == list(exact)
    for index, point in enumerate(payload["points"]):
        assert set(point) == BASE.BASELINE_POINT_KEYS
        assert point["start"] == (NOW + timedelta(minutes=30 * index)).isoformat()
        assert point["end"] == (
            NOW + timedelta(minutes=30 * (index + 1))
        ).isoformat()


def test_boolean_and_nonfinite_inputs_fail_closed_per_series() -> None:
    payload = build(
        input_changes={
            "current_soc_percent": True,
            "battery_capacity_kwh": float("nan"),
        }
    )
    assert payload["current_actual_soc_percent"] is None
    assert all(point["soc_end_percent"] is None for point in payload["points"])
    assert all(point["pv_kw"] is not None for point in payload["points"])
    assert all(point["load_kw"] is not None for point in payload["points"])


def test_output_contract_keeps_exact_neutral_model_parameters() -> None:
    payload = build(
        input_changes={
            "pv_to_battery_efficiency": 0.81,
            "battery_to_home_efficiency": 0.87,
            "maximum_charge_power_kw": 3.25,
            "maximum_discharge_power_kw": 4.75,
            "system_ac_power_kw": 12.5,
        }
    )
    assert payload["pv_to_battery_efficiency"] == 0.81
    assert payload["battery_to_home_efficiency"] == 0.87
    assert payload["maximum_charge_power_kw"] == 3.25
    assert payload["maximum_discharge_power_kw"] == 4.75
    assert payload["system_ac_power_kw"] == 12.5

    unavailable = build(input_changes={"maximum_charge_power_kw": None})
    assert unavailable["maximum_charge_power_kw"] is None
    assert unavailable["state"] == "partial"
    assert all(
        point["soc_end_percent"] is None for point in unavailable["points"]
    )


def test_system_ac_bridge_is_shared_by_load_charge_and_export() -> None:
    """Potential PV cannot exceed the exact installation-wide AC bridge."""

    point = build(
        input_changes={
            "battery_capacity_kwh": 100.0,
            "current_soc_percent": 50.0,
            "pv_to_battery_efficiency": 1.0,
            "maximum_charge_power_kw": 100.0,
            "system_ac_power_kw": 10.0,
        },
        slot_values=((20.0, 4.0),),
    )["points"][0]
    # The 30-minute bridge carries 2 kWh to LOAD, leaving 3 kWh for charging.
    # The remaining 1 kWh of raw PV cannot be exported through a full bridge.
    assert point["battery_kw"] == 6.0
    assert point["soc_end_percent"] == 53.0
    assert point["grid_export_kw"] == 0.0

    deficit = build(
        input_changes={
            "battery_capacity_kwh": 100.0,
            "current_soc_percent": 100.0,
            "reserve_soc_percent": 0.0,
            "hardware_minimum_soc_percent": 0.0,
            "battery_to_home_efficiency": 0.95,
            "maximum_discharge_power_kw": 20.0,
            "system_ac_power_kw": 10.0,
        },
        slot_values=((0.0, 15.0),),
    )["points"][0]
    assert deficit["battery_kw"] == -10.0
    assert deficit["grid_import_kw"] == 5.0
    assert abs(deficit["soc_end_percent"] - 94.7368) <= 1e-6

    unavailable = build(input_changes={"system_ac_power_kw": None})
    assert unavailable["system_ac_power_kw"] is None
    assert unavailable["state"] == "partial"
    assert all(
        point["soc_end_percent"] is None for point in unavailable["points"]
    )


def test_system_ac_bridge_routes_pv_and_battery_to_load_before_grid() -> None:
    """PV and battery can supply LOAD only through their shared AC bridge."""

    saturated = build(
        input_changes={
            "battery_capacity_kwh": 100.0,
            "current_soc_percent": 100.0,
            "reserve_soc_percent": 0.0,
            "hardware_minimum_soc_percent": 0.0,
            "pv_to_battery_efficiency": 1.0,
            "battery_to_home_efficiency": 1.0,
            "maximum_charge_power_kw": 100.0,
            "maximum_discharge_power_kw": 100.0,
            "system_ac_power_kw": 10.0,
        },
        slot_values=((20.0, 15.0),),
    )["points"][0]
    assert saturated["battery_kw"] == 0.0
    assert saturated["grid_import_kw"] == 5.0
    assert saturated["grid_export_kw"] == 0.0
    pv_to_load_kwh = (15.0 - saturated["grid_import_kw"]) * 0.5
    assert pv_to_load_kwh == 5.0
    assert 20.0 * 0.5 - pv_to_load_kwh == 5.0

    deficit = build(
        input_changes={
            "battery_capacity_kwh": 100.0,
            "current_soc_percent": 100.0,
            "reserve_soc_percent": 0.0,
            "hardware_minimum_soc_percent": 0.0,
            "battery_to_home_efficiency": 1.0,
            "maximum_discharge_power_kw": 100.0,
            "system_ac_power_kw": 10.0,
        },
        slot_values=((8.0, 15.0),),
    )["points"][0]
    assert deficit["battery_kw"] == -2.0
    assert deficit["grid_import_kw"] == 5.0
    assert deficit["grid_export_kw"] == 0.0
    pv_to_load_kwh = 8.0 * 0.5
    battery_to_load_kwh = -deficit["battery_kw"] * 0.5
    assert pv_to_load_kwh == 4.0
    assert battery_to_load_kwh == 1.0
    assert pv_to_load_kwh + battery_to_load_kwh == 5.0


def test_invalid_slot_time_is_rejected() -> None:
    invalid = slots(((1.0, 1.0),))[0]
    invalid = replace(invalid, start=invalid.start.replace(tzinfo=None))
    try:
        BASE.build_baseline_energy_timeline(inputs(), [invalid])
    except ValueError as err:
        assert "timezone-aware" in str(err)
    else:
        raise AssertionError("naive baseline timestamp was accepted")


def test_gap_between_slots_is_rejected_without_carrying_soc() -> None:
    forecast_slots = slots(((1.0, 1.0), (1.0, 1.0)))
    forecast_slots[1] = replace(
        forecast_slots[1],
        start=forecast_slots[1].start + timedelta(minutes=30),
        end=forecast_slots[1].end + timedelta(minutes=30),
    )
    try:
        BASE.build_baseline_energy_timeline(inputs(), forecast_slots)
    except ValueError as err:
        assert "contiguous" in str(err)
    else:
        raise AssertionError("baseline SOC was allowed to bridge a slot gap")


def main() -> None:
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"Baseline energy timeline: {len(tests)} deterministic checks passed.")


if __name__ == "__main__":
    main()

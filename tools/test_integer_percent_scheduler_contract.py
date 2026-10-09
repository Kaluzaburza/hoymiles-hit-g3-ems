"""New scheduler setpoints use whole percent; saved readbacks remain lossless."""

from __future__ import annotations

import math
from pathlib import Path

from jinja2 import Environment, StrictUndefined
import yaml


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "home_assistant/hoymiles_ems_scheduler.yaml"


def _walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _finite_number(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def main() -> None:
    package = yaml.safe_load(PACKAGE.read_text(encoding="utf-8"))
    helpers = package["input_number"]
    for entity in (
        "hoymiles_rce_requested_discharge_power",
        "hoymiles_tariff_requested_charge_power",
        "hoymiles_rcm_latched_pre_discharge_target_soc",
        "hoymiles_rcm_latched_pre_discharge_power",
    ):
        assert helpers[entity]["step"] == 1, entity

    # These fields store actual old settings, not new operator setpoints.
    for entity in (
        "hoymiles_tariff_saved_max_charge_power",
        "hoymiles_rce_saved_max_discharge_power",
        "hoymiles_rcm_saved_battery_charge_power",
        "hoymiles_rcm_saved_export_limit",
        "hoymiles_rcm_saved_max_discharge_power",
        "hoymiles_battery_balancing_saved_charge_power",
    ):
        assert helpers[entity]["step"] == 0.1, entity

    environment = Environment(undefined=StrictUndefined)
    sensor = next(
        item for item in _walk(package["template"])
        if item.get("unique_id") == "hoymiles_rce_effective_discharge_power_percent"
    )
    cases = (
        (50.0, 50.0, 50.0),
        (50.0, 49.9, 49.0),
        (49.9, 50.0, 49.0),
        (100.0, 92.631, 92.0),
        (100.0, 0.999, 0.0),
        (0.999, 100.0, 0.0),
        (100.0, 1.0, 1.0),
        (100.0, 0.0, 0.0),
        (100.0, 100.0, 100.0),
    )
    for requested, planned, expected in cases:
        values = {
            "input_number.hoymiles_rce_requested_discharge_power": requested,
            "sensor.hoymiles_rce_bms_safe_discharge_power": 99.0,
        }
        attributes = {
            "system_power_kw": 10.0,
            "current_slot_execution_power_percent": planned,
        }
        context = {
            "states": lambda entity: values[entity],
            "state_attr": lambda _entity, key: attributes[key],
            "is_number": _finite_number,
        }
        assert environment.from_string(sensor["availability"]).render(context).strip() == "True"
        actual = float(environment.from_string(sensor["state"]).render(context))
        assert actual == expected, (requested, planned, actual)
        assert actual.is_integer() and actual <= min(requested, planned, 100.0)

    # The existing source-readiness contract still withholds unavailable values.
    for invalid in (None, "unknown", float("nan"), float("inf")):
        for missing_field in ("requested", "planned", "bms", "system"):
            values = {
                "input_number.hoymiles_rce_requested_discharge_power":
                    invalid if missing_field == "requested" else 50.0,
                "sensor.hoymiles_rce_bms_safe_discharge_power":
                    invalid if missing_field == "bms" else 10.0,
            }
            attributes = {
                "system_power_kw": invalid if missing_field == "system" else 10.0,
                "current_slot_execution_power_percent":
                    invalid if missing_field == "planned" else 50.0,
            }
            context = {
                "states": lambda entity: values[entity],
                "state_attr": lambda _entity, key: attributes[key],
                "is_number": _finite_number,
            }
            assert environment.from_string(sensor["availability"]).render(context).strip() == "False"

    def latched_template(entity: str) -> str:
        writes = [
            item["data"]["value"] for item in _walk(package["automation"])
            if item.get("action") == "input_number.set_value"
            and item.get("target", {}).get("entity_id") == entity
        ]
        assert len(writes) == 1, (entity, writes)
        return writes[0]

    power = latched_template("input_number.hoymiles_rcm_latched_pre_discharge_power")
    soc = latched_template("input_number.hoymiles_rcm_latched_pre_discharge_target_soc")
    assert float(environment.from_string(power).render(power_percent=49.9)) == 49.0
    assert float(environment.from_string(power).render(power_percent=0.9)) == 0.0
    assert float(environment.from_string(soc).render(target_soc=49.1)) == 50.0

    # Public selectors request whole numbers. Their existing low-level scripts
    # also service legacy restores, so they must not silently floor saved 49.9.
    for script in (
        "hoymiles_verified_set_ems_maximum_charge_power",
        "hoymiles_verified_set_ems_maximum_discharge_power",
        "hoymiles_verified_set_battery_max_charge_power",
        "hoymiles_verified_set_gcf_export_limit",
    ):
        body = package["script"][script]
        assert body["fields"]["value"]["selector"]["number"]["step"] == 1
        writes = [
            item["data"]["value"] for item in _walk(body["sequence"])
            if item.get("action") == "number.set_value"
        ]
        assert len(writes) == 1
        assert float(environment.from_string(writes[0]).render(value=49.9)) == 49.9

    print("Integer scheduler contract: 9 capped setpoints, 16 unavailable inputs, "
          "3 RCEm latches, integer UI and faithful saved-value routes PASS")


if __name__ == "__main__":
    main()

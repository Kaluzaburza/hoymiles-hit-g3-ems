"""Offline contract for the neutral Shared EMS helper package."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "home_assistant" / "hoymiles_ems_shared_inputs.yaml"


def main() -> None:
    document = yaml.safe_load(PACKAGE.read_text(encoding="utf-8"))
    assert set(document) == {"input_text", "input_number", "input_select"}

    assert set(document["input_text"]) == {
        "hoymiles_ems_pv_forecast_today_entity",
        "hoymiles_ems_pv_forecast_tomorrow_entity",
        "hoymiles_ems_pv_forecast_day_3_entity",
    }
    for helper in document["input_text"].values():
        assert helper["max"] == 255

    numbers = document["input_number"]
    assert set(numbers) == {
        "hoymiles_ems_fallback_daily_home_load",
        "hoymiles_ems_pv_to_battery_efficiency",
        "hoymiles_ems_battery_to_home_efficiency",
    }
    assert numbers["hoymiles_ems_fallback_daily_home_load"] == {
        "name": "EMS — zastępcze dobowe zużycie domu",
        "min": 0,
        "max": 200,
        "step": 0.5,
        "mode": "box",
        "unit_of_measurement": "kWh",
        "icon": "mdi:home-lightning-bolt-outline",
    }
    assert numbers["hoymiles_ems_pv_to_battery_efficiency"] == {
        "name": "EMS — sprawność modelu PV → magazyn",
        "min": 0,
        "max": 100,
        "step": 1,
        "mode": "box",
        "unit_of_measurement": "%",
        "icon": "mdi:battery-arrow-up-outline",
    }
    assert numbers["hoymiles_ems_battery_to_home_efficiency"] == {
        "name": "EMS — sprawność modelu magazyn → dom",
        "min": 0,
        "max": 100,
        "step": 1,
        "mode": "box",
        "unit_of_measurement": "%",
        "icon": "mdi:home-import-outline",
    }

    rated = document["input_select"]
    assert set(rated) == {"hoymiles_ems_inverter_rated_power_each"}
    assert rated["hoymiles_ems_inverter_rated_power_each"]["options"] == [
        "Automatycznie",
        "5 kW",
        "8 kW",
        "10 kW",
        "12 kW",
        "15 kW",
        "20 kW",
    ]
    assert "initial" not in rated["hoymiles_ems_inverter_rated_power_each"]
    assert all("initial" not in helper for helper in numbers.values())

    raw = PACKAGE.read_text(encoding="utf-8")
    for forbidden in (
        "automation:",
        "script:",
        "service:",
        "modbus:",
        "4300",
        "4306",
        "hoymiles_rce_inverter_rated_power:",
        "hoymiles_rce_fallback_daily_load:",
    ):
        assert forbidden not in raw

    print("Shared EMS helper package: neutral efficiency contract passed")


if __name__ == "__main__":
    main()

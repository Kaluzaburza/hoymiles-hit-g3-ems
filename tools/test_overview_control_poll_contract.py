"""Focused contract for the single-inverter Overview control poll."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class Loader(yaml.SafeLoader):
    """Loader retaining ESPHome lambda bodies as strings."""


Loader.add_constructor("!lambda", lambda loader, node: loader.construct_scalar(node))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    overview_path = ROOT / "packages" / "overview.yaml"
    overview = yaml.load(overview_path.read_text(encoding="utf-8"), Loader=Loader)
    sources = [
        sensor
        for sensor in overview["sensor"]
        if sensor.get("platform") == "modbus_controller"
    ]
    expected = [
        ("pv_total_power_master_30001", 30001, 2),
        ("internal_pv_total_power_30003", 30003, 2),
        ("external_pv_total_power_30005", 30005, 2),
        ("inv_active_power_master_30007", 30007, 2),
        ("battery_power_master_30009", 30009, 2),
        ("grid_total_active_power_master_30011", 30011, 2),
        ("generator_active_power_30013", 30013, 2),
        ("load_active_power_master_30015", 30015, 2),
        ("smart_load_active_power_30017", 30017, 2),
        ("battery_soc_master_30020", 30020, 1),
    ]

    check(len(sources) == len(expected), "Overview physical source membership changed")
    for source, (expected_id, expected_address, expected_count) in zip(
        sorted(sources, key=lambda item: item["address"]), expected, strict=True
    ):
        check(source.get("id") == expected_id, f"Unexpected Overview source at {expected_address}")
        check(source.get("address") == expected_address, f"Overview range moved at {expected_id}")
        check(source.get("register_type") == "read", f"{expected_id} changed from FC04")
        check(
            source.get("modbus_controller_id") == "${modbus_control_controller_id}",
            f"{expected_id} is outside the existing 5 s control poll",
        )
        default_count = 1 if source.get("value_type") == "U_WORD" else 2
        check(
            source.get("register_count", default_count) == expected_count,
            f"Overview span changed at {expected_id}",
        )
        check("register_count" not in source, f"{expected_id} retains deprecated register_count")
        check(not source.get("force_new_range", False), f"{expected_id} split the FC04 range")
        check(source.get("skip_updates", 0) == 0, f"{expected_id} skips control cycles")

    by_id = {sensor.get("id"): sensor for sensor in overview["sensor"]}
    cursor = expected[0][1]
    for sensor_id, address, count in expected:
        if address != cursor:
            check(
                sensor_id == "battery_soc_master_30020"
                and address == cursor + 1
                and by_id[sensor_id].get("reuse_previous_range") is True,
                "Only SOC may bridge the ignored 30019 word in the shared FC04 range",
            )
        cursor = address + count
    check(cursor == 30021, "Overview no longer covers exactly registers 30001-30020")

    smart_load = by_id["smart_load_active_power_30017"]
    soc = by_id["battery_soc_master_30020"]
    check(smart_load["value_type"] == "S_DWORD_R", "Smart Load lost signed reversed DWORD decoding")
    check(smart_load["address"] - 30001 == 16, "Smart Load byte offset is not 32")
    check((smart_load["address"] - 30001) * 2 == 32, "Smart Load response offset changed")
    check((soc["address"] - 30001) * 2 == 38, "SOC response offset is not 38")
    check(soc.get("reuse_previous_range") is True, "SOC no longer joins the previous FC04 range")

    for raw_id, public_id in [
        ("pv_total_power_master_30001", "pv_total_power_30001"),
        ("battery_power_master_30009", "battery_power_30009"),
        ("grid_total_active_power_master_30011", "grid_total_active_power_30011"),
        ("load_active_power_master_30015", "load_active_power_30015"),
        ("battery_soc_master_30020", "battery_soc_30020"),
    ]:
        source = by_id[raw_id]
        public = by_id[public_id]
        body = source["on_value"]["then"][0]["lambda"]
        check(source.get("force_update") is True, f"Physical reports disabled for {raw_id}")
        check(f"id({public_id}).publish_state(x);" in body, f"{public_id} lost physical publication")
        check("topology_readback == 0" in body and "> 60000U" in body, f"{public_id} lost topology freshness")
        check("!= 1" in body, f"{public_id} can overwrite the parallel aggregate")
        check(public.get("update_interval") == "never", f"{public_id} gained a republish timer")
        check(public.get("force_update") is True, f"{public_id} stopped publishing generations")

    inverter = (ROOT / "hoymiles-inverter.yaml").read_text(encoding="utf-8")
    for line in (
        '  fast_update_interval: "13s"',
        '  control_update_interval: "5s"',
        '  settings_update_interval: "20s"',
        '  update_interval: "150s"',
    ):
        check(line in inverter, f"Unrelated controller cadence changed: {line.strip()}")

    connection = (ROOT / "packages" / "modbus_connection.yaml").read_text(encoding="utf-8")
    check("  turnaround_time: 100ms" in connection, "Accepted 100 ms turnaround is missing")
    transport = yaml.safe_load(connection)
    check(transport["modbus"]["send_wait_time"] == "250ms", "Response wait changed")
    controllers = transport["modbus_controller"]
    check(len(controllers) == 5, "Shared bus controller membership changed")
    slow = [c for c in controllers if c['id'] == '${modbus_slow_controller_id}']
    check(len(slow) == 1 and slow[0]['update_interval'] == '${slow_update_interval}',
          'Static diagnostics lost the separate slow poll')
    for controller in controllers:
        check(controller["modbus_id"] == transport["modbus"]["id"], "Controller bypasses shared spacing")
        check("command_throttle" not in controller, "Deprecated no-op command_throttle returned")
    print("Overview control poll: FC04 30001-30020 / 5 s contract passed")


if __name__ == "__main__":
    main()

"""Validate real public YAMLs with local packages; no release tag or secrets needed.

Run with the CI-pinned ESPHome environment. --compile selects a complete public
firmware profile; --compile-flow-control is the compatible manual-profile alias. Temporary
configuration/build files are removed when the process exits normally.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import yaml


ROOT = Path(__file__).resolve().parents[1]
ENTRIES = (
    "hoymiles-inverter.yaml",
    "hoymiles-inverter-flow-control.yaml",
    "examples/esphome/hoymiles-hit-g3.yaml",
    "hoymiles-inverter-s3.yaml",
)
SECRETS = """wifi_ssid: docs-validation-network
wifi_password: docs-validation-password
fallback_password: docs-validation-fallback
api_key: MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=
"""


def local_fixture(source: str) -> str:
    """Replace only the remote package wrapper, retaining every device option."""
    pattern = (
        r"(?m)^packages:\n  hoymiles_hit_g3:\n"
        r"    url: https://github.com/Kaluzaburza/hoymiles-hit-g3-ems\n"
        r"    ref: v1\.5\.8RC2\n    refresh: 1d\n    files:\n"
        r"(?:      - packages/[a-z0-9_]+\.yaml\n)+"
    )
    matches = list(re.finditer(pattern, source))
    assert len(matches) == 1, "Expected one version-pinned public package block"
    paths = re.findall(r"- (packages/[a-z0-9_]+\.yaml)", matches[0].group())
    expected = {
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "packages").glob("*.yaml")
        if not p.name.startswith("optional_")
    }
    assert set(paths) == expected and len(paths) == len(expected)
    replacement = "packages:\n" + "".join(
        f'  {Path(path).stem}: !include "{(ROOT / path).as_posix()}"\n'
        for path in paths
    )
    return source[: matches[0].start()] + replacement + source[matches[0].end() :]


def main() -> None:
    from esphome.config import read_config
    from esphome.core import CORE

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-flow-control", action="store_true")
    parser.add_argument("--compile", choices=("esp32", "flow-control", "s3"))
    args = parser.parse_args()
    sources = {name: (ROOT / name).read_text(encoding="utf-8") for name in ENTRIES}

    # The new entry point may add direction control and its own import metadata
    # only. All other device options, identities and package pins must agree.
    standard = yaml.load(sources[ENTRIES[0]], Loader=yaml.BaseLoader)
    manual = yaml.load(sources[ENTRIES[1]], Loader=yaml.BaseLoader)
    assert manual.pop("uart") == {
        "id": "modbus_uart",
        "flow_control_pin": {"number": "${uart_flow_control_pin}", "inverted": "false"},
    }
    assert manual["substitutions"].pop("uart_flow_control_pin") == "GPIO4"
    assert manual["dashboard_import"]["package_import_url"] == (
        "github://Kaluzaburza/hoymiles-hit-g3-ems/"
        "hoymiles-inverter-flow-control.yaml@v1.5.8RC2"
    )
    manual["dashboard_import"] = standard["dashboard_import"]
    assert manual == standard, "Manual variant changes more than UART direction/import metadata"

    s3 = yaml.load(sources[ENTRIES[3]], Loader=yaml.BaseLoader)
    assert s3.pop("psram") == {"mode": "octal", "speed": "80MHz"}
    assert s3["esp32"] == {
        "board": "${board}", "variant": "ESP32S3", "flash_size": "16MB",
        "framework": {"type": "esp-idf"},
    }
    assert s3["substitutions"]["board"] == "esp32-s3-devkitc-1"
    assert s3["dashboard_import"]["package_import_url"].endswith(
        "/hoymiles-inverter-s3.yaml@v1.5.8RC2"
    )
    s3["esp32"] = standard["esp32"]
    s3["substitutions"]["board"] = standard["substitutions"]["board"]
    s3["dashboard_import"] = standard["dashboard_import"]
    assert s3 == standard, "S3 changes more than board/memory/import metadata"

    scratch = Path(tempfile.gettempdir()) / "hoymiles-entry-validation"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="esphome-entry-check-", dir=scratch) as temp:
        work = Path(temp)
        (work / "secrets.yaml").write_text(SECRETS, encoding="utf-8")
        fixtures = {}
        for name, source in sources.items():
            fixture = work / Path(name).name
            fixture.write_text(local_fixture(source), encoding="utf-8")
            fixtures[name] = fixture
            CORE.reset()
            CORE.config_path = fixture
            config = read_config({})
            assert config is not None, f"ESPHome rejected {name}"
            assert len(config["uart"]) == 1, f"Duplicate UART in {name}"
            uart = config["uart"][0]
            assert str(uart["id"]) == "modbus_uart"
            assert uart["baud_rate"] == 115200
            assert uart["tx_pin"]["number"] == 17
            assert uart["rx_pin"]["number"] == 16
            assert uart["data_bits"] == 8 and uart["stop_bits"] == 1
            assert uart["parity"] == "NONE"
            assert len(config["modbus"]) == 1
            assert str(config["modbus"][0]["uart_id"]) == "modbus_uart"
            assert len(config["modbus_controller"]) == 5
            if name == ENTRIES[3]:
                assert config["esp32"]["variant"] == "ESP32S3"
                assert config["esp32"]["flash_size"] == "16MB"
                assert config["psram"]["mode"] == "octal"
            if name == ENTRIES[1]:
                assert uart["flow_control_pin"]["number"] == 4
                assert uart["flow_control_pin"]["inverted"] is False
            else:
                assert "flow_control_pin" not in uart
            print(f"PASS: {name}: complete local packages, one UART and one Modbus hub")

        # Reproduce #33 with the same canonical mapping. The test must not pass
        # merely because the obsolete list/!extend example was silently accepted.
        broken = work / "issue-33-invalid.yaml"
        broken.write_text(
            local_fixture(sources[ENTRIES[0]])
            + "\nuart:\n  - id: !extend modbus_uart\n"
            + "    flow_control_pin:\n      number: GPIO4\n      inverted: false\n",
            encoding="utf-8",
        )
        CORE.reset()
        CORE.config_path = broken
        output = io.StringIO()
        with redirect_stdout(output):
            rejected = read_config({})
        assert rejected is None
        assert "Source for extension of ID 'modbus_uart' was not found" in output.getvalue()
        print("PASS: issue #33 reproduced; obsolete list/!extend form rejected")
        selected = "flow-control" if args.compile_flow_control else args.compile
        if selected:
            selected_entry = {"esp32": ENTRIES[0], "flow-control": ENTRIES[1], "s3": ENTRIES[3]}[selected]
            subprocess.run(
                [sys.executable, "-m", "esphome", "compile", str(fixtures[selected_entry])],
                cwd=ROOT,
                check=True,
            )
            print(f"PASS: complete {selected} firmware compiled; no upload performed")


if __name__ == "__main__":
    main()

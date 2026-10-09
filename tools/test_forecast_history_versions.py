"""Package-version compatibility must not bypass physical PV history gates."""

from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "forecast_history_versions", ROOT / "custom_components/hoymiles_hit_modbus/forecast_model.py"
)
assert SPEC is not None and SPEC.loader is not None
MODEL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODEL
SPEC.loader.exec_module(MODEL)


def main():
    start = datetime(2026, 10, 4, tzinfo=ZoneInfo("Europe/Warsaw"))
    end = start + timedelta(days=1)
    eligible = MODEL.forecast_learning_history_day_eligible

    def check(version, expected):
        actual = eligible([(start, "on")], [(start, version)], day_start=start, day_end=end)
        assert actual is expected, (version, actual, expected)

    scheduler = (ROOT / "home_assistant/hoymiles_ems_scheduler.yaml").read_text(encoding="utf-8")
    package = re.search(r'name: "Hoymiles EMS Package Version".*?state: "([^"]+)"', scheduler, re.S)
    assert package, "Package version entity must remain discoverable"
    check(package[1], True)
    for version in ("1.5.2", "1.5.7", "1.5.8", "1.5.8rc2", "1.5.8RC2", " 1.5.8RC2 ",
                    "1.5.8rc1", "1.5.8rc999", "1.5.8-rc2", "1.5.8+build.109", "1.5.8rc2+build.109"):
        check(version, True)
    for version in ("1.5.1", "1.5.1rc2", "1.5.8rc", "1.5.8rc0", "1.5.8rc02", "1.5.8rc1000",
                    "1.5.8garbage", "1.5.8rc2garbage", "1.5.8rc2.109", "v1.5.8rc2", "1.5", "1.5.8\nrc2",
                    "unknown", "unavailable", "", None, "1.5.8+" + "x" * 65):
        check(version, False)
    count = 29
    for date in ((2026, 10, 4), (2026, 3, 29), (2026, 10, 25)):
        start = datetime(*date, tzinfo=ZoneInfo("Europe/Warsaw"))
        end = start + timedelta(days=1)
        mid = start + timedelta(hours=12)
        assert eligible([(start, "on")], [(start, "1.5.7"), (mid, "1.5.8RC2")], day_start=start, day_end=end)
        for state in ("off", "unknown", "unavailable"):
            assert not eligible([(start, "on"), (mid, state)], [(start, "1.5.8rc2")], day_start=start, day_end=end)
        for state in ("1.5.1", "unknown", "unavailable", "1.5.8rc2garbage"):
            assert not eligible([(start, "on")], [(start, "1.5.8rc2"), (mid, state)], day_start=start, day_end=end)
        for export, version in (([], [(start, "1.5.8rc2")]), ([(start, "on")], []),
                                ([(mid, "on")], [(start, "1.5.8rc2")]), ([(start, "on")], [(mid, "1.5.8rc2")])):
            assert not eligible(export, version, day_start=start, day_end=end)
        count += 12
    assert not eligible([(start, "on")], [(start, "1.5.8rc2")], day_start=end, day_end=start)
    print(f"PV history versions: {count + 1} checks PASS; emitted package, stable/RC, malformed, export/coverage, DST")


if __name__ == "__main__":
    main()

"""A06 regression tests for post-R09 result ownership."""

from __future__ import annotations

import asyncio  # Load stdlib select before the integration's select.py is visible.
from dataclasses import replace
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "custom_components" / "hoymiles_hit_modbus"))

from test_rce_self_consumption_filter import VerifiedSchedule, filtered_case
from test_rce_optimizer import base_input
from rce_optimizer import SLOT, optimize_rce


def _assert_selected_result(result: object, capacity_kwh: float) -> None:
    assert result.timeline_trace is not None and result.timeline_trace.points
    trace_end = result.timeline_trace.points[-1].soc_percent * capacity_kwh / 100.0
    selected = result.self_consumption_shadow.selected
    assert selected is not None
    assert abs(result.ending_battery_kwh - trace_end) < 1e-6
    assert abs(result.ending_battery_kwh - selected.ending_battery_kwh_dc) < 1e-6
    assert abs(result.terminal_energy_value_pln - selected.terminal_value_pln) < 1e-6


def main() -> None:
    partial = filtered_case()
    assert 0.0 < partial.planned_export_kwh < partial.legacy_planned_export_kwh
    _assert_selected_result(partial, 20.0)

    # No-filter control: all three representations already agree.
    unfiltered = optimize_rce(
        replace(
            base_input(),
            tariff_price_schedule=VerifiedSchedule(0.01),
            self_consumption_filter_enabled=True,
        )
    )
    if unfiltered.self_consumption_shadow.selected is not None:
        _assert_selected_result(unfiltered, 20.0)

    fully_rejected = filtered_case(schedule=None)
    assert fully_rejected.legacy_planned_export_kwh > 0.0
    assert fully_rejected.planned_export_kwh == 0.0
    assert fully_rejected.timeline_trace is not None
    trace_end = fully_rejected.timeline_trace.points[-1].soc_percent * 20.0 / 100.0
    assert abs(fully_rejected.ending_battery_kwh - trace_end) < 1e-6

    seed = filtered_case()
    pv = {
        seed.timeline_trace.points[index].end - SLOT: 2.0
        for index in range(2, 4)
    }
    risk = filtered_case(
        pv_by_slot_kwh=pv,
        conservative_pv_by_slot_kwh={},
    )
    expected_trace_end = risk.timeline_trace.points[-1].soc_percent * 20.0 / 100.0
    assert risk.ending_battery_kwh <= expected_trace_end + 1e-9
    assert risk.self_consumption_shadow.selected is not None
    assert abs(
        risk.terminal_energy_value_pln
        - risk.self_consumption_shadow.selected.terminal_value_pln
    ) < 1e-6
    print("RCE filtered aggregates: 4 selected-plan consistency scenarios passed")


if __name__ == "__main__":
    main()

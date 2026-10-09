"""Observation-only comparison of RCE sale and retained home energy.

The comparator is deliberately smaller than a planner.  It evaluates a fixed
set of proportional reductions of an already feasible RCE export candidate.
It cannot add a slot, move a deadline, increase export, or charge from grid.
Every variant owns its battery trajectory from the same physical initial SOC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from time import perf_counter
from typing import Callable, Iterable


LEGACY_SALE_CONTRACT = "rce_sale_profit_v1"
SHADOW_VALUE_CONTRACT = "rce_self_consumption_shadow_v3"
ENERGY_TOLERANCE_KWH = 1e-6
COST_TOLERANCE_PLN = 0.01
DEFAULT_SWITCH_COST_PLN = 0.05
DEFAULT_UNCERTAINTY_FRACTION = 0.10
DEFAULT_MINIMUM_ADVANTAGE_PLN = 0.10
DEFAULT_SCALE_STEPS = 20


@dataclass(frozen=True, slots=True)
class ShadowSlot:
    """One immutable half-hour input shared by all compared variants."""

    start: datetime
    end: datetime
    load_kwh_ac: float
    pv_kwh_ac: float
    baseline_export_kwh_ac: float
    sell_price_pln_kwh_ac: float | None
    import_price_pln_kwh_ac: float | None
    protected_floor_kwh_dc: float
    # The export reserve protects energy from a voluntary sale.  It is not the
    # physical floor for supplying the house.  ``None`` keeps compatibility
    # with old standalone callers; the production adapter always publishes the
    # actual hard floor explicitly.
    hard_floor_kwh_dc: float | None = None


@dataclass(frozen=True, slots=True)
class ShadowVariant:
    """One complete battery trajectory for a bounded export scale."""

    export_scale: float
    feasible: bool
    forced_export_kwh_ac: float
    preserved_export_kwh_ac: float
    export_revenue_pln: float
    natural_export_revenue_pln: float
    grid_import_kwh_ac: float
    grid_import_cost_pln: float
    battery_to_load_kwh_ac: float
    avoided_import_value_pln: float
    battery_throughput_kwh_dc: float
    battery_wear_cost_pln: float
    ending_battery_kwh_dc: float
    terminal_energy_kwh_dc: float
    terminal_value_pln: float
    raw_objective_pln: float
    balance_error_kwh: float
    slot_exports_kwh_ac: tuple[float, ...]
    natural_export_kwh_ac: float = 0.0
    slot_grid_import_kwh_ac: tuple[float, ...] = ()
    slot_natural_exports_kwh_ac: tuple[float, ...] = ()
    slot_battery_after_kwh_dc: tuple[float, ...] = ()


@dataclass(frozen=True, slots=True)
class ShadowEvaluation:
    """Versioned diagnostic result; it never grants execution authority."""

    available: bool
    status_code: str
    reason_code: str
    baseline: ShadowVariant | None
    selected: ShadowVariant | None
    variants: tuple[ShadowVariant, ...]
    adjusted_advantage_vs_sale_pln: float | None
    maximum_justified_export_kwh_ac: float | None
    preserved_for_home_kwh_ac: float | None
    runtime_ms: float
    price_source_id: str | None = None
    price_source_revision: str | None = None
    price_quality: str | None = None

    def as_attributes(self) -> dict[str, object]:
        """Return a compact HA-safe publication separate from sale-profit."""

        baseline = self.baseline
        selected = self.selected
        return {
            "shadow_contract_version": SHADOW_VALUE_CONTRACT,
            "legacy_sale_contract_version": LEGACY_SALE_CONTRACT,
            "shadow_control_applied": False,
            "shadow_available": self.available,
            "shadow_status_code": self.status_code,
            "shadow_reason_code": self.reason_code,
            "shadow_price_source_id": self.price_source_id,
            "shadow_price_source_revision": self.price_source_revision,
            "shadow_price_quality": self.price_quality,
            "shadow_variant_count": len(self.variants),
            "shadow_baseline_export_kwh": _rounded(
                baseline.forced_export_kwh_ac if baseline else None
            ),
            "shadow_baseline_export_revenue_pln": _rounded(
                baseline.export_revenue_pln if baseline else None
            ),
            "shadow_baseline_natural_export_revenue_pln": _rounded(
                baseline.natural_export_revenue_pln if baseline else None
            ),
            "shadow_baseline_grid_import_kwh": _rounded(
                baseline.grid_import_kwh_ac if baseline else None
            ),
            "shadow_baseline_natural_export_kwh": _rounded(
                baseline.natural_export_kwh_ac if baseline else None
            ),
            "shadow_baseline_ending_battery_kwh": _rounded(
                baseline.ending_battery_kwh_dc if baseline else None
            ),
            "shadow_baseline_objective_pln": _rounded(
                baseline.raw_objective_pln if baseline else None
            ),
            "shadow_baseline_grid_import_cost_pln": _rounded(
                baseline.grid_import_cost_pln if baseline else None
            ),
            "shadow_baseline_terminal_energy_kwh": _rounded(
                baseline.terminal_energy_kwh_dc if baseline else None
            ),
            "shadow_baseline_terminal_value_pln": _rounded(
                baseline.terminal_value_pln if baseline else None
            ),
            "shadow_selected_export_scale": _rounded(
                selected.export_scale if selected else None, 4
            ),
            "shadow_maximum_justified_export_kwh": _rounded(
                self.maximum_justified_export_kwh_ac
            ),
            "shadow_preserved_for_home_kwh": _rounded(
                self.preserved_for_home_kwh_ac
            ),
            "shadow_selected_export_revenue_pln": _rounded(
                selected.export_revenue_pln if selected else None
            ),
            "shadow_selected_natural_export_revenue_pln": _rounded(
                selected.natural_export_revenue_pln if selected else None
            ),
            "shadow_selected_grid_import_kwh": _rounded(
                selected.grid_import_kwh_ac if selected else None
            ),
            "shadow_selected_natural_export_kwh": _rounded(
                selected.natural_export_kwh_ac if selected else None
            ),
            "shadow_selected_ending_battery_kwh": _rounded(
                selected.ending_battery_kwh_dc if selected else None
            ),
            "shadow_selected_avoided_import_value_pln": _rounded(
                selected.avoided_import_value_pln if selected else None
            ),
            "shadow_selected_avoided_import_cost_vs_sale_pln": _rounded(
                (
                    baseline.grid_import_cost_pln
                    - selected.grid_import_cost_pln
                )
                if baseline and selected
                else None
            ),
            "shadow_selected_grid_import_cost_pln": _rounded(
                selected.grid_import_cost_pln if selected else None
            ),
            "shadow_selected_battery_wear_cost_pln": _rounded(
                selected.battery_wear_cost_pln if selected else None
            ),
            "shadow_selected_terminal_value_pln": _rounded(
                selected.terminal_value_pln if selected else None
            ),
            "shadow_selected_terminal_energy_kwh": _rounded(
                selected.terminal_energy_kwh_dc if selected else None
            ),
            "shadow_selected_objective_pln": _rounded(
                selected.raw_objective_pln if selected else None
            ),
            "shadow_adjusted_advantage_vs_sale_pln": _rounded(
                self.adjusted_advantage_vs_sale_pln
            ),
            "shadow_energy_balance_error_kwh": _rounded(
                selected.balance_error_kwh if selected else None, 8
            ),
            "shadow_new_export_slots": 0,
            "shadow_new_grid_charge_kwh": 0.0,
            "shadow_runtime_ms": _rounded(self.runtime_ms, 3),
            "shadow_prediction_not_realized_saving": True,
        }


def unavailable_shadow_evaluation(
    reason_code: str,
    *,
    price_source_id: str | None = None,
    price_source_revision: str | None = None,
    price_quality: str | None = None,
) -> ShadowEvaluation:
    """Build one explicit unavailable result without treating it as zero."""

    return ShadowEvaluation(
        available=False,
        status_code="unavailable",
        reason_code=reason_code,
        baseline=None,
        selected=None,
        variants=(),
        adjusted_advantage_vs_sale_pln=None,
        maximum_justified_export_kwh_ac=None,
        preserved_for_home_kwh_ac=None,
        runtime_ms=0.0,
        price_source_id=price_source_id,
        price_source_revision=price_source_revision,
        price_quality=price_quality,
    )


def _rounded(value: float | None, digits: int = 3) -> float | None:
    return round(value, digits) if value is not None else None


def _finite_nonnegative(value: float, name: str) -> float:
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0.0:
        raise ValueError(f"{name}_invalid")
    return numeric


def _efficiency(value: float, name: str) -> float:
    numeric = float(value) / 100.0
    if not math.isfinite(numeric) or not 0.0 < numeric <= 1.0:
        raise ValueError(f"{name}_invalid")
    return numeric


def _validate_slots(slots: tuple[ShadowSlot, ...]) -> str | None:
    previous_end: datetime | None = None
    for slot in slots:
        if (
            slot.start.tzinfo is None
            or slot.start.utcoffset() is None
            or slot.end.tzinfo is None
            or slot.end.utcoffset() is None
            or slot.end <= slot.start
            or (previous_end is not None and slot.start < previous_end)
        ):
            return "invalid_horizon"
        previous_end = slot.end
        for name, value in (
            ("load", slot.load_kwh_ac),
            ("pv", slot.pv_kwh_ac),
            ("baseline_export", slot.baseline_export_kwh_ac),
            ("protected_floor", slot.protected_floor_kwh_dc),
        ):
            try:
                _finite_nonnegative(value, name)
            except ValueError:
                return f"invalid_{name}"
        if slot.hard_floor_kwh_dc is not None:
            try:
                _finite_nonnegative(slot.hard_floor_kwh_dc, "hard_floor")
            except ValueError:
                return "invalid_hard_floor"
        if slot.import_price_pln_kwh_ac is None:
            return "import_price_missing"
        if not math.isfinite(float(slot.import_price_pln_kwh_ac)):
            return "import_price_invalid"
        if slot.baseline_export_kwh_ac > ENERGY_TOLERANCE_KWH:
            if slot.sell_price_pln_kwh_ac is None:
                return "sell_price_missing"
            if not math.isfinite(float(slot.sell_price_pln_kwh_ac)):
                return "sell_price_invalid"
    return None


def _simulate_variant(
    slots: tuple[ShadowSlot, ...],
    *,
    export_scale: float,
    initial_battery_kwh_dc: float,
    battery_capacity_kwh_dc: float,
    export_efficiency: float,
    charge_efficiency: float,
    house_efficiency: float,
    battery_wear_cost_pln_kwh_dc: float,
    terminal_target_kwh_dc: float,
    terminal_value_pln_kwh_dc: float,
) -> ShadowVariant:
    battery = initial_battery_kwh_dc
    initial = battery
    forced_export = 0.0
    baseline_export = sum(slot.baseline_export_kwh_ac for slot in slots)
    export_revenue = 0.0
    grid_import = 0.0
    grid_import_cost = 0.0
    battery_to_load = 0.0
    avoided_import_value = 0.0
    throughput = 0.0
    charged_dc = 0.0
    slot_exports: list[float] = []
    slot_imports: list[float] = []
    slot_battery_after: list[float] = []
    feasible = True

    for slot in slots:
        hard_floor = min(
            max(
                slot.protected_floor_kwh_dc
                if slot.hard_floor_kwh_dc is None
                else slot.hard_floor_kwh_dc,
                0.0,
            ),
            battery_capacity_kwh_dc,
        )
        export_floor = min(
            max(slot.protected_floor_kwh_dc, hard_floor),
            battery_capacity_kwh_dc,
        )
        load = slot.load_kwh_ac
        pv = slot.pv_kwh_ac
        direct_pv = min(load, pv)
        remaining_load = max(load - direct_pv, 0.0)
        remaining_pv = max(pv - direct_pv, 0.0)

        deliverable = max(battery - hard_floor, 0.0) * house_efficiency
        from_battery = min(remaining_load, deliverable)
        battery -= from_battery / house_efficiency
        throughput += from_battery / house_efficiency
        battery_to_load += from_battery
        import_energy = max(remaining_load - from_battery, 0.0)
        import_price = float(slot.import_price_pln_kwh_ac)
        grid_import += import_energy
        grid_import_cost += import_energy * import_price
        avoided_import_value += from_battery * import_price
        slot_imports.append(import_energy)

        desired_export = slot.baseline_export_kwh_ac * export_scale
        export_available = max(battery - export_floor, 0.0) * export_efficiency
        if desired_export > export_available + ENERGY_TOLERANCE_KWH:
            feasible = False
            desired_export = max(min(desired_export, export_available), 0.0)
        battery -= desired_export / export_efficiency
        throughput += desired_export / export_efficiency
        forced_export += desired_export
        export_revenue += desired_export * float(slot.sell_price_pln_kwh_ac or 0.0)
        slot_exports.append(desired_export)

        charge_ac = min(
            remaining_pv,
            max(battery_capacity_kwh_dc - battery, 0.0) / charge_efficiency,
        )
        charged = charge_ac * charge_efficiency
        battery += charged
        charged_dc += charged
        battery = min(max(battery, 0.0), battery_capacity_kwh_dc)
        slot_battery_after.append(battery)

    final_floor = (
        min(
            max(
                slots[-1].protected_floor_kwh_dc
                if slots[-1].hard_floor_kwh_dc is None
                else slots[-1].hard_floor_kwh_dc,
                0.0,
            ),
            battery_capacity_kwh_dc,
        )
        if slots
        else 0.0
    )
    terminal_eligible = min(
        max(battery - final_floor, 0.0),
        max(terminal_target_kwh_dc, 0.0),
    )
    terminal_value = terminal_eligible * max(terminal_value_pln_kwh_dc, 0.0)
    wear = throughput * battery_wear_cost_pln_kwh_dc
    objective = export_revenue - grid_import_cost - wear + terminal_value
    accounted_end = initial + charged_dc - throughput
    balance_error = battery - accounted_end
    return ShadowVariant(
        export_scale=export_scale,
        feasible=feasible and abs(balance_error) <= ENERGY_TOLERANCE_KWH,
        forced_export_kwh_ac=forced_export,
        preserved_export_kwh_ac=max(baseline_export - forced_export, 0.0),
        export_revenue_pln=export_revenue,
        natural_export_revenue_pln=0.0,
        grid_import_kwh_ac=grid_import,
        grid_import_cost_pln=grid_import_cost,
        battery_to_load_kwh_ac=battery_to_load,
        avoided_import_value_pln=avoided_import_value,
        battery_throughput_kwh_dc=throughput,
        battery_wear_cost_pln=wear,
        ending_battery_kwh_dc=battery,
        terminal_energy_kwh_dc=terminal_eligible,
        terminal_value_pln=terminal_value,
        raw_objective_pln=objective,
        balance_error_kwh=balance_error,
        slot_exports_kwh_ac=tuple(slot_exports),
        slot_grid_import_kwh_ac=tuple(slot_imports),
        slot_natural_exports_kwh_ac=tuple(0.0 for _ in slots),
        slot_battery_after_kwh_dc=tuple(slot_battery_after),
    )


def evaluate_sale_vs_preserve(
    slots: Iterable[ShadowSlot],
    *,
    initial_battery_kwh_dc: float,
    battery_capacity_kwh_dc: float,
    export_efficiency_percent: float,
    charge_efficiency_percent: float,
    house_discharge_efficiency_percent: float,
    battery_wear_cost_pln_kwh_dc: float = 0.0,
    terminal_target_kwh_dc: float = 0.0,
    terminal_value_pln_kwh_dc: float = 0.0,
    uncertainty_fraction: float = DEFAULT_UNCERTAINTY_FRACTION,
    switch_cost_pln: float = DEFAULT_SWITCH_COST_PLN,
    minimum_advantage_pln: float = DEFAULT_MINIMUM_ADVANTAGE_PLN,
    scale_steps: int = DEFAULT_SCALE_STEPS,
    price_source_id: str | None = None,
    price_source_revision: str | None = None,
    price_quality: str | None = None,
    variant_simulator: Callable[[float], ShadowVariant] | None = None,
) -> ShadowEvaluation:
    """Compare bounded reductions without changing the original RCE candidate."""

    started = perf_counter()
    frozen = tuple(slots)
    invalid = _validate_slots(frozen)
    if invalid is not None:
        return ShadowEvaluation(
            False,
            "unavailable",
            invalid,
            None,
            None,
            (),
            None,
            None,
            None,
            (perf_counter() - started) * 1000.0,
            price_source_id,
            price_source_revision,
            price_quality,
        )
    try:
        capacity = _finite_nonnegative(battery_capacity_kwh_dc, "capacity")
        initial = _finite_nonnegative(initial_battery_kwh_dc, "initial_battery")
        wear = _finite_nonnegative(battery_wear_cost_pln_kwh_dc, "wear_cost")
        export_eff = _efficiency(export_efficiency_percent, "export_efficiency")
        charge_eff = _efficiency(charge_efficiency_percent, "charge_efficiency")
        house_eff = _efficiency(
            house_discharge_efficiency_percent,
            "house_discharge_efficiency",
        )
    except ValueError as err:
        return ShadowEvaluation(
            False, "unavailable", str(err), None, None, (), None, None, None,
            (perf_counter() - started) * 1000.0,
            price_source_id, price_source_revision, price_quality,
        )
    if capacity <= 0.0 or initial > capacity + ENERGY_TOLERANCE_KWH:
        return ShadowEvaluation(
            False, "unavailable", "battery_state_invalid", None, None, (),
            None, None, None, (perf_counter() - started) * 1000.0,
            price_source_id, price_source_revision, price_quality,
        )
    if (
        not math.isfinite(uncertainty_fraction)
        or not 0.0 <= uncertainty_fraction <= 1.0
        or not math.isfinite(switch_cost_pln)
        or switch_cost_pln < 0.0
        or not math.isfinite(minimum_advantage_pln)
        or minimum_advantage_pln < 0.0
        or isinstance(scale_steps, bool)
        or scale_steps < 1
        or scale_steps > 100
    ):
        return ShadowEvaluation(
            False, "unavailable", "comparison_settings_invalid", None, None,
            (), None, None, None, (perf_counter() - started) * 1000.0,
            price_source_id, price_source_revision, price_quality,
        )

    variants = tuple(
        (
            variant_simulator(step / scale_steps)
            if variant_simulator is not None
            else _simulate_variant(
                frozen,
                export_scale=step / scale_steps,
                initial_battery_kwh_dc=initial,
                battery_capacity_kwh_dc=capacity,
                export_efficiency=export_eff,
                charge_efficiency=charge_eff,
                house_efficiency=house_eff,
                battery_wear_cost_pln_kwh_dc=wear,
                terminal_target_kwh_dc=terminal_target_kwh_dc,
                terminal_value_pln_kwh_dc=terminal_value_pln_kwh_dc,
            )
        )
        for step in range(scale_steps + 1)
    )
    baseline = variants[-1]
    if not baseline.feasible:
        return ShadowEvaluation(
            False, "unavailable", "baseline_not_feasible", baseline, None,
            variants, None, None, None, (perf_counter() - started) * 1000.0,
            price_source_id, price_source_revision, price_quality,
        )
    baseline_export = baseline.forced_export_kwh_ac
    if baseline_export <= ENERGY_TOLERANCE_KWH:
        return ShadowEvaluation(
            True, "no_existing_export", "no_forced_battery_export", baseline,
            baseline, variants, 0.0, 0.0, 0.0,
            (perf_counter() - started) * 1000.0,
            price_source_id, price_source_revision, price_quality,
        )

    ranked: list[tuple[float, ShadowVariant]] = []
    for variant in variants:
        if not variant.feasible:
            continue
        additional_avoided_import = max(
            baseline.grid_import_cost_pln - variant.grid_import_cost_pln,
            0.0,
        )
        adjusted = variant.raw_objective_pln - baseline.raw_objective_pln
        if variant.export_scale < 1.0 - ENERGY_TOLERANCE_KWH:
            adjusted -= switch_cost_pln
            adjusted -= uncertainty_fraction * additional_avoided_import
        ranked.append((adjusted, variant))

    best_adjusted = max(value for value, _ in ranked)
    if (
        best_adjusted <= COST_TOLERANCE_PLN
        or best_adjusted < minimum_advantage_pln - COST_TOLERANCE_PLN
    ):
        selected = baseline
        advantage = 0.0
        status = "legacy_sale"
        reason = "sale_value_exceeds_preservation"
    else:
        near_best = [
            variant
            for adjusted, variant in ranked
            if adjusted >= best_adjusted - COST_TOLERANCE_PLN
            and adjusted >= minimum_advantage_pln - COST_TOLERANCE_PLN
        ]
        selected = max(near_best, key=lambda item: item.forced_export_kwh_ac)
        additional_avoided_import = max(
            baseline.grid_import_cost_pln - selected.grid_import_cost_pln,
            0.0,
        )
        advantage = (
            selected.raw_objective_pln
            - baseline.raw_objective_pln
            - switch_cost_pln
            - uncertainty_fraction * additional_avoided_import
        )
        if selected.forced_export_kwh_ac <= ENERGY_TOLERANCE_KWH:
            status = "preserve_home"
            reason = "retained_energy_avoids_higher_import"
        else:
            status = "sell_surplus"
            reason = "only_surplus_above_avoided_import_is_sold"

    return ShadowEvaluation(
        True,
        status,
        reason,
        baseline,
        selected,
        variants,
        advantage,
        selected.forced_export_kwh_ac,
        selected.preserved_export_kwh_ac,
        (perf_counter() - started) * 1000.0,
        price_source_id,
        price_source_revision,
        price_quality,
    )

"""The shared RCE/Pstryk bounded whole-horizon sale block search.

Each caller supplies its chronological
physics and objective; price, day-prefix, removal, exchange and tie refinements
have a single implementation. This module does not read HA or persist samples.
"""
from datetime import datetime, timezone as dt_timezone
import math
from typing import Iterable, Mapping

EXACT_TIE_ROUNDOFF_PLN = 1e-12

def select_sale_blocks(*, candidates, now, battery_wear_cost_pln_kwh,
        export_efficiency, baseline_objective, feasible, objective,
        slot_physical_cap, exact_objective, current_slot_start=None,
        slot_minimum_export=None, sale_suppresses_refill=False):
    if not candidates:
        return {}

    minimum = {start: slot_minimum_export(start) if slot_minimum_export else .01
               for _, start in candidates}
    original_feasible, original_objective, original_exact = feasible, objective, exact_objective

    def executable(plan):
        return all(energy == 0.0 or energy >= minimum.get(start, math.inf) - 1e-9
                   for start, energy in plan.items())

    def feasible(plan):
        return executable(plan) and original_feasible(plan)

    def objective(plan):
        return original_objective(plan) if executable(plan) else -math.inf

    def exact_objective(plan):
        return original_exact(plan) if executable(plan) else -math.inf

    candidate_by_start = {start: slot for slot, start in candidates}
    price_order = [
        start
        for _, start in sorted(
            candidates,
            key=lambda item: (-item[0].price_pln_kwh, item[1]),
        )
    ]
    reverse_tie_price_order = [
        start
        for _, start in sorted(
            candidates,
            key=lambda item: (
                -item[0].price_pln_kwh,
                -item[1].timestamp(),
            ),
        )
    ]
    short_horizon = len(candidates) <= 7
    # Padding rows which cannot cover battery wear are common in a complete
    # 48-hour PSE response.  Keep every row in seeds and physical simulations,
    # but rank the bounded exchange set by direct marginal net value.  Lower
    # priced rows which are active in a strong seed are added back below; they
    # can still be valuable indirectly by creating later PV headroom.
    direct_break_even_price = (
        max(battery_wear_cost_pln_kwh, 0.0)
        / max(export_efficiency, 0.01)
    )
    profitable_price_order = [
        start
        for start in price_order
        if candidate_by_start[start].price_pln_kwh
        > direct_break_even_price + 1e-9
    ]
    chronological = sorted(candidate_by_start)

    def normalized(plan: Mapping[datetime, float]) -> dict[datetime, float]:
        return {
            start: max(float(energy), 0.0)
            for start, energy in plan.items()
            if energy >= 0.001
        }

    def signature(plan: Mapping[datetime, float]) -> tuple[tuple[int, int], ...]:
        return tuple(
            (int(start.timestamp()), round(energy * 10000.0))
            for start, energy in sorted(normalized(plan).items())
        )

    def maximum_feasible(
        base: Mapping[datetime, float],
        start: datetime,
        minimum_only: bool = False,
    ) -> float:
        low = minimum[start]
        high = min(low, slot_physical_cap(start)) if minimum_only else slot_physical_cap(start)
        if high < low:
            return 0.0
        full_trial = dict(base)
        full_trial[start] = high
        if feasible(full_trial):
            return high
        minimum_trial = dict(base)
        minimum_trial[start] = low
        if not feasible(minimum_trial):
            return 0.0
        # A coarse fixed iteration count left material residual energy whenever
        # the physical slot cap was large (for example 1.125 kWh from a 23 kWh
        # budget).  Resolve every boundary below 0.01 kWh.  Twelve probes are
        # the minimum for long horizons; exceptionally large caps receive only
        # the few additional probes mathematically required by their range.
        precision_iterations = max(
            12,
            math.ceil(math.log2(max(high / 0.01, 1.0))),
        )
        for _ in range(precision_iterations):
            middle = (low + high) / 2.0
            trial = dict(base)
            trial[start] = middle
            if feasible(trial):
                low = middle
            else:
                high = middle
        return low

    def grow(
        order: Iterable[datetime],
        active: set[datetime] | None = None,
        minimum_only: bool = False,
    ) -> dict[datetime, float]:
        plan: dict[datetime, float] = {}
        for start in order:
            if active is not None and start not in active:
                continue
            energy = maximum_feasible(plan, start, minimum_only)
            if energy >= 0.01:
                plan[start] = energy
        return plan

    # Complete active-set enumeration is practical for short horizons and is
    # also the release-test oracle path.  Longer horizons receive deterministic
    # bases at every distinct market-price boundary.
    seeds: dict[tuple[tuple[int, int], ...], tuple[float, dict[datetime, float]]] = {}
    daily_seeds: dict[tuple[tuple[int, int], ...], tuple[float, dict[datetime, float]]] = {}
    interior_seeds: dict[tuple[tuple[int, int], ...], tuple[float, dict[datetime, float]]] = {}

    def remember(plan: Mapping[datetime, float], destination=None) -> None:
        destination = seeds if destination is None else destination
        clean = normalized(plan)
        value = objective(clean)
        key = signature(clean)
        previous = destination.get(key)
        if previous is None or value > previous[0]:
            destination[key] = (value, clean)

    remember({})
    remember(grow(chronological))
    if short_horizon:
        remember(grow(price_order))
        remember(grow(reversed(chronological)))
        for mask in range(1, 1 << len(price_order)):
            active = {
                start
                for index, start in enumerate(price_order)
                if mask & (1 << index)
            }
            remember(grow(price_order, active))
            remember(grow(chronological, active))
            # Mode-5 sale suppresses same-slot PV refill. Filling the first
            # coordinate to its maximum can therefore hide a better pair of
            # partial sales. Keep bounded interior seeds for the existing
            # active-set search; every seed still uses whole-horizon physics.
            if sale_suppresses_refill:
                remember(grow(chronological, active, minimum_only=True), interior_seeds)
    else:
        # One price-ordered pass produces every threshold prefix.  Remembering
        # the plan whenever the price changes costs no extra feasibility
        # searches and prevents a profitable middle price band from vanishing
        # between only the maximum/minimum thresholds.
        # A daily refill can fund both evenings. Global descending prices can
        # reserve tomorrow's battery first and miss that feasible combination.
        # Use one shared whole-horizon budget, with prices ranked within each
        # local day, as an additional seed (never independent daily budgets).
        day_price_order = sorted(price_order, key=lambda stamp: (
            stamp.astimezone(now.tzinfo).date(),
            -candidate_by_start[stamp].price_pln_kwh, stamp,
        ))
        day_price_prefix: dict[datetime, float] = {}
        for start in day_price_order:
            energy = maximum_feasible(day_price_prefix, start)
            if energy >= 0.01:
                day_price_prefix[start] = energy
                clean = normalized(day_price_prefix)
                daily_seeds[signature(clean)] = (objective(clean), clean)
        price_prefix: dict[datetime, float] = {}
        for index, start in enumerate(price_order):
            energy = maximum_feasible(price_prefix, start)
            if energy >= 0.01:
                price_prefix[start] = energy
            current_price = candidate_by_start[start].price_pln_kwh
            next_price = (
                candidate_by_start[price_order[index + 1]].price_pln_kwh
                if index + 1 < len(price_order)
                else None
            )
            if next_price != current_price:
                remember(price_prefix)

        # Equal-price slots are not interchangeable when an early export can
        # consume battery headroom which later PV would otherwise refill.  A
        # second threshold pass with reversed chronological tie-breaking is a
        # linear, deterministic hedge against that coupling on real 48-hour
        # horizons; it does not enumerate active sets.
        reverse_price_prefix: dict[datetime, float] = {}
        for index, start in enumerate(reverse_tie_price_order):
            energy = maximum_feasible(reverse_price_prefix, start)
            if energy >= 0.01:
                reverse_price_prefix[start] = energy
            current_price = candidate_by_start[start].price_pln_kwh
            next_price = (
                candidate_by_start[
                    reverse_tie_price_order[index + 1]
                ].price_pln_kwh
                if index + 1 < len(reverse_tie_price_order)
                else None
            )
            if next_price != current_price:
                remember(reverse_price_prefix)

    def optimize_coordinate(
        original: Mapping[datetime, float],
        order: Iterable[datetime],
        *,
        passes: int = 2,
    ) -> tuple[float, dict[datetime, float]]:
        plan = normalized(original)
        current_value = objective(plan)
        for _ in range(max(passes, 1)):
            changed = False
            for start in order:
                base = dict(plan)
                old_energy = base.pop(start, 0.0)
                high = maximum_feasible(base, start)
                if high < 0.001:
                    candidate_energy = 0.0
                    candidate_value = objective(base)
                else:
                    # Natural PV spill creates non-concave one-dimensional
                    # sections.  Scan the complete bounded interval first,
                    # then refine around its best section.
                    grid = [high * index / 8.0 for index in range(9)]
                    # Never worsen a seed solely because its existing
                    # continuous amount lies between coarse grid points.
                    if 0.0 < old_energy < high:
                        grid.append(old_energy)
                    if minimum[start] <= high:
                        grid.append(minimum[start])
                    grid = sorted(set(grid))
                    values = []
                    for energy in grid:
                        trial = dict(base)
                        if energy >= 0.001:
                            trial[start] = energy
                        values.append(objective(trial))
                    best_index = max(range(len(grid)), key=values.__getitem__)
                    left = grid[max(best_index - 1, 0)]
                    right = grid[min(best_index + 1, len(grid) - 1)]
                    for _ in range(12):
                        first = left + (right - left) / 3.0
                        second = right - (right - left) / 3.0
                        first_trial = dict(base)
                        second_trial = dict(base)
                        if first >= 0.001:
                            first_trial[start] = first
                        if second >= 0.001:
                            second_trial[start] = second
                        if objective(first_trial) < objective(second_trial):
                            left = first
                        else:
                            right = second
                    choices = (
                        0.0,
                        old_energy,
                        high,
                        left,
                        (left + right) / 2.0,
                        right,
                    )
                    candidate_energy = 0.0
                    candidate_value = -math.inf
                    for energy in choices:
                        trial = dict(base)
                        if energy >= 0.001:
                            trial[start] = energy
                        value = objective(trial)
                        if value > candidate_value:
                            candidate_value = value
                            candidate_energy = energy
                if candidate_energy >= 0.01:
                    base[start] = candidate_energy
                plan = base
                if abs(candidate_energy - old_energy) >= 0.005:
                    changed = True
                current_value = candidate_value
            if not changed:
                break
        return current_value, normalized(plan)

    # Refine only the strongest distinct bases; this bounds HA update latency
    # independently of the number of PSE rows.
    # Short/medium horizons have only a handful of deterministic seeds.  Keep
    # enough of them for refinement so a middle-price active set is not
    # discarded merely because its unrefined boundary plan ranks below two
    # extreme-price seeds.  Real 48-hour horizons stay capped at two, which is
    # the part that controls Home Assistant event-loop latency.
    strongest_count = (
        len(seeds) if short_horizon else (8 if len(candidates) <= 10 else 2)
    )
    strongest = sorted(seeds.values(), key=lambda item: item[0], reverse=True)[
        :strongest_count
    ]
    # Keep every legacy refinement candidate. An additional daily seed must
    # not displace an older seed whose refinement would have been better.
    # At most one extra refinement bounds the additional worker latency.
    if daily_seeds:
        daily_best = max(daily_seeds.values(), key=lambda item: item[0])
        if not any(signature(plan) == signature(daily_best[1]) for _, plan in strongest):
            strongest.append(daily_best)
    # Minimum executable commands explore the discontinuity between Self-Use
    # refill and Mode-5 export. Keep the old seeds: a new unrefined candidate
    # must not evict a stronger legacy refinement.
    interior_best = sorted(interior_seeds.values(), key=lambda item: item[0], reverse=True)
    for candidate in (interior_best if short_horizon else interior_best[:1]):
        if not any(signature(plan) == signature(candidate[1]) for _, plan in strongest):
            strongest.append(candidate)
    best_value = baseline_objective
    best_plan: dict[datetime, float] = {}
    for seed_value, seed in strongest:
        if seed_value > best_value + 0.0001:
            best_value = seed_value
            best_plan = seed
        # On a real 48-hour horizon, optimizing every empty coordinate would
        # make runtime scale with all PSE rows.  Threshold seeds already choose
        # the active set; refine only their active amounts in a single pass.
        # This is enough to avoid a full-slot, below-wear discharge when only a
        # partial export is needed to create PV headroom.
        if len(candidates) <= 10:
            coordinate_order = price_order
            passes = 2
        else:
            # Bound long-horizon work independently of market-row count.  The
            # economically relevant partial-headroom correction is on the top
            # active sale branches; lower-price active amounts remain at their
            # already feasible seed boundaries.
            coordinate_order = [
                start for start in price_order if start in seed
            ][:2]
            passes = 1
        orders: tuple[Iterable[datetime], ...] = (coordinate_order,)
        for order in orders:
            value, plan = optimize_coordinate(seed, order, passes=passes)
            if value > best_value + 0.0001:
                best_value = value
                best_plan = plan

    # Build a genuinely bounded active/relevant exchange set.  At most six
    # low-value active coordinates are candidates for removal; the remaining
    # places go first to profitable inactive rows, then to rows active in an
    # alternative strong seed and immediate temporal neighbours.  The final
    # highest-price fallback also covers a below-wear headroom opportunity
    # without letting dozens of zero/small-positive padding rows disable the
    # exchange.  Six active places are intentional: a few profitable tail
    # rows can otherwise consume all low-price ranks and hide the earlier
    # export whose removal restores expected PV value.  Four inactive places
    # remain available while the total search set stays capped at ten.
    active_order = sorted(
        best_plan,
        key=lambda start: (
            candidate_by_start[start].price_pln_kwh,
            start,
        ),
    )
    inactive_priority: list[datetime] = []
    inactive_seen: set[datetime] = set()

    def add_inactive(start: datetime) -> None:
        if start in best_plan or start in inactive_seen:
            return
        inactive_seen.add(start)
        inactive_priority.append(start)

    for start in profitable_price_order:
        add_inactive(start)
    for _, seed in strongest:
        for start in price_order:
            if start in seed:
                add_inactive(start)
    chronological_index = {
        start: index for index, start in enumerate(chronological)
    }
    for active in active_order:
        index = chronological_index[active]
        if index > 0:
            add_inactive(chronological[index - 1])
        if index + 1 < len(chronological):
            add_inactive(chronological[index + 1])
    for start in price_order:
        add_inactive(start)

    active_limit = min(len(active_order), 6)
    selected_pair_starts = active_order[:active_limit]
    selected_pair_starts.extend(
        inactive_priority[: 10 - len(selected_pair_starts)]
    )
    pair_starts = sorted(selected_pair_starts)
    pair_price_spread = (
        max(candidate_by_start[start].price_pln_kwh for start in pair_starts)
        - min(candidate_by_start[start].price_pln_kwh for start in pair_starts)
        if pair_starts
        else 0.0
    )
    # Runtime is bounded by ``pair_starts`` itself, not by the number of rows
    # whose price happens to clear the wear threshold.  A normal 48-hour PSE
    # payload may contain dozens of barely-above-wear padding rows; they must
    # not switch off refinement of the genuinely relevant active/inactive
    # coordinates selected above.
    sparse_exchange = not short_horizon and 2 <= len(pair_starts) <= 10

    # A threshold seed is grown to every feasible boundary.  Under different
    # conservative/expected PV trajectories, that can retain an export which
    # is physically safe but destroys expected natural-export value.  A swap
    # search cannot remove it unless a useful inactive coordinate exists.
    # Prune at most the six bounded low-value active rows first, accepting
    # only strict whole-horizon improvements.  This is a tiny linear local
    # search (at most 6 + 5 + ... + 1 objective evaluations), not an active-set
    # enumeration.  If pruning changed the plan, refine only the surviving
    # coordinates from the same bounded set so newly released PV/battery
    # headroom can be assigned continuously.
    pruned = False
    if sparse_exchange:
        for _ in range(active_limit):
            removal_value = best_value
            removal_plan: dict[datetime, float] | None = None
            for start in pair_starts:
                if start not in best_plan:
                    continue
                trial = dict(best_plan)
                trial.pop(start, None)
                value = objective(trial)
                if value > removal_value + 0.0001:
                    removal_value = value
                    removal_plan = trial
            if removal_plan is None:
                break
            best_value = removal_value
            best_plan = removal_plan
            pruned = True
        if pruned:
            refinement_order = [
                start for start in pair_starts if start in best_plan
            ]
            if refinement_order:
                refined_value, refined_plan = optimize_coordinate(
                    best_plan,
                    refinement_order,
                    passes=1,
                )
                if refined_value > best_value + 0.0001:
                    best_value = refined_value
                    best_plan = refined_plan

    # The exchange search remains bounded even when the complete market input
    # contains many directly profitable rows: only ``pair_starts`` (at most
    # ten relevant coordinates) participates.
    if (
        sparse_exchange
        # Equal-price timing is already covered by both chronological tie
        # orders above.  Repeating every active/inactive exchange in that case
        # cannot improve direct sale value and consumed most of the 48-hour
        # runtime budget in flat price bands.
        and pair_price_spread > 1e-9
        and any(start in best_plan for start in pair_starts)
        and any(start not in best_plan for start in pair_starts)
    ):
        # Coordinate descent cannot cross a valley where one early export must
        # be removed at the same time as a later slot is added.  Search only
        # active/inactive exchanges inside the bounded relevant set.  Each
        # trial grows both coordinates to their exact feasible boundary; the
        # single winning active set is then continuously refined.  This avoids
        # running the expensive one-dimensional scan for every padded market
        # row while preserving a runtime linear in the simulated horizon.
        # Medium horizons can require two consecutive exchanges to cross a
        # three-coordinate valley.  Keep that exhaustive-on-the-bounded-set
        # behaviour for <=10 rows; real 48-hour inputs get one pass.
        exchange_passes = (
            2
            if len(candidates) <= 10
            or any(
                candidate_by_start[start].price_pln_kwh
                <= direct_break_even_price + 1e-9
                for start in pair_starts
            )
            else 1
        )
        for _ in range(exchange_passes):
            pass_value = best_value
            pass_plan = best_plan
            pass_order: tuple[datetime, datetime] | None = None
            active_starts = [
                start for start in pair_starts if start in best_plan
            ]
            inactive_starts = [
                start for start in pair_starts if start not in best_plan
            ]
            for first in active_starts:
                for second in inactive_starts:
                    base = dict(best_plan)
                    base.pop(first, None)
                    base.pop(second, None)
                    for order in ((first, second), (second, first)):
                        plan = dict(base)
                        for start in order:
                            energy = maximum_feasible(plan, start)
                            if energy >= 0.01:
                                plan[start] = energy
                        value = objective(plan)
                        if value > pass_value + 0.0001:
                            pass_value = value
                            pass_plan = plan
                            pass_order = order
            if pass_value <= best_value + 0.0001:
                break
            refined_value, refined_plan = optimize_coordinate(
                pass_plan,
                pass_order or pair_starts,
                passes=1,
            )
            if refined_value > pass_value + 0.0001:
                pass_value = refined_value
                pass_plan = refined_plan
            best_value = pass_value
            best_plan = pass_plan
    if not short_horizon:
        # Keep the complete incumbent above, including coordinate refinement,
        # pruning and bounded exchanges.  Independent local-day prefixes are
        # additional whole-horizon alternatives only: they cannot displace a
        # stronger legacy result and they never combine separately-budgeted
        # daily plans.  This covers a search gap where a small expensive sale
        # on a later day could anchor every global price prefix and hide the
        # better active set on an earlier day.
        planning_timezone = now.tzinfo or dt_timezone.utc
        local_days = sorted(
            {start.astimezone(planning_timezone).date() for start in price_order}
        )
        daily_orders_seen: set[tuple[datetime, ...]] = set()
        minimum_seed_attempts = 0
        for local_day in local_days:
            # One deterministic price/UTC order is sufficient here.  The
            # legacy global path above already keeps its reverse tie hedge;
            # duplicating it per day would make the added work scale twice
            # with every market row without expanding price-prefix coverage.
            for complete_order in (price_order,):
                daily_order = tuple(
                    start
                    for start in complete_order
                    if start.astimezone(planning_timezone).date() == local_day
                )
                if not daily_order or daily_order in daily_orders_seen:
                    continue
                daily_orders_seen.add(daily_order)
                daily_prefix: dict[datetime, float] = {}
                for index, start in enumerate(daily_order):
                    energy = maximum_feasible(daily_prefix, start)
                    if energy >= 0.01:
                        daily_prefix[start] = energy
                    elif (minimum_seed_attempts < 4 and 0 < len(daily_prefix) <= 10
                          and minimum[start] <= slot_physical_cap(start)):
                        # A remaining physical budget can fit a mathematical
                        # tail but not a whole executable command. Reserve that
                        # command first and refill the same earlier choices;
                        # do not simply discard the tail or relax its minimum.
                        # Four complete alternatives, each at most ten rows,
                        # bound the extra search independently of market size.
                        probe = dict(daily_prefix)
                        probe[start] = minimum[start] / 2.
                        if original_feasible(probe):
                            trial = {start: minimum[start]}
                            if feasible(trial):
                                minimum_seed_attempts += 1
                                for prior in daily_prefix:
                                    amount = maximum_feasible(trial, prior)
                                    if amount >= .01:
                                        trial[prior] = amount
                                trial_value = objective(trial)
                                if trial_value > best_value + .0001:
                                    best_value, best_plan = trial_value, trial
                    current_price = candidate_by_start[start].price_pln_kwh
                    next_price = (
                        candidate_by_start[daily_order[index + 1]].price_pln_kwh
                        if index + 1 < len(daily_order)
                        else None
                    )
                    if next_price == current_price:
                        continue
                    value = objective(daily_prefix)
                    if value > best_value + 0.0001:
                        best_value = value
                        best_plan = dict(daily_prefix)
    if not short_horizon:
        # Price prefixes retain earlier choices, and the bounded exchange set
        # can omit a feasible later window (or fail to replace two early sales
        # together). Compare independent single-slot plans after refinement so
        # adding alternatives never discards the incumbent's refined result.
        # Keep the same whole-horizon reserve, PV scenarios and objective.
        for start in chronological:
            candidate = grow((start,))
            value = objective(candidate)
            if value > best_value + 0.0001:
                best_value = value
                best_plan = candidate
    if not short_horizon and sale_suppresses_refill:
        # The Mode-5 refill discontinuity can require changing several sale
        # amounts together. Explore a bounded beam on ten highest-price rows;
        # all other rows remain covered by the incumbent search above. Every
        # branch is a complete, feasible horizon, never an independent budget.
        beam = [(baseline_objective, {})]
        beam_starts = price_order[:10]
        half_cap = max(slot_physical_cap(s) for s in beam_starts) / 2.
        for start in beam_starts:
            alternatives = {}
            cap = slot_physical_cap(start)
            for _, base in beam:
                for energy in (0., minimum[start], min(cap, half_cap), cap):
                    trial = dict(base)
                    if energy >= .01:
                        trial[start] = energy
                    value = objective(trial)
                    if math.isfinite(value):
                        alternatives[signature(trial)] = (value, trial)
            beam = sorted(alternatives.values(), key=lambda item: item[0], reverse=True)[:8]
        if beam:
            value, plan = optimize_coordinate(beam[0][1],
                [start for start in beam_starts if start in beam[0][1]], passes=1)
        else:
            value, plan = -math.inf, {}
        if value > best_value + .0001:
            best_value, best_plan = value, plan
    # Equal-price timing is a deterministic tie, not an economic reason to
    # discard the current half-hour. A later-only seed can win the bounded
    # search even when shifting its energy into the current slot is feasible
    # and has exactly the same whole-horizon objective. Check one such shift
    # on the same fresh physics and prices; never accept any objective loss.
    current_start = current_slot_start or now.replace(minute=now.minute//30*30, second=0, microsecond=0).astimezone(dt_timezone.utc)
    if current_start in candidate_by_start and best_plan.get(current_start, 0.0) < 0.01:
        current_cap = slot_physical_cap(current_start)
        if current_cap >= 0.01:
            for later_start in sorted(best_plan):
                if (
                    later_start <= current_start
                    or candidate_by_start[later_start].price_pln_kwh
                    != candidate_by_start[current_start].price_pln_kwh
                ):
                    continue
                moved = min(current_cap, best_plan[later_start])
                if moved < 0.01:
                    continue
                trial = dict(best_plan)
                trial[current_start] = moved
                trial[later_start] -= moved
                if trial[later_start] < 0.001:
                    trial.pop(later_start)
                trial_value = exact_objective(trial)
                if (
                    trial_value != -math.inf
                    and trial_value + EXACT_TIE_ROUNDOFF_PLN >= exact_objective(best_plan)
                ):
                    best_plan = trial
                break
    return best_plan

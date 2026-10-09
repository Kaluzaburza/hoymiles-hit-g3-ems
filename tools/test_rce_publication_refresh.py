"""Production RCE publication re-attests equivalent numeric reports."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta
from types import SimpleNamespace

import test_rce_lease_real_cadence as fixture


ENTITY = "sensor.hoymiles_hit_battery_voltage_bms"


async def main() -> None:
    h = fixture.h
    h.CLOCK["now"] = fixture.NOW
    hass, _, _, _, source, module, _, _ = await fixture.solver_probe()
    energy = h._load("rce_publication_energy_data", h.COMPONENT / "energy_data.py")
    module.numeric_state_sample = energy.numeric_state_sample
    calls = []
    hass.states.values[ENTITY] = h.FakeState("51.2", reported=fixture.NOW)

    async def executor_with_reports(func, *args):
        result = func(*args)
        for _ in range(6):
            h.CLOCK["now"] += timedelta(seconds=3)
            hass.states.values[ENTITY] = h.FakeState("51.2", reported=h.CLOCK["now"])
            await asyncio.sleep(0)
        calls.append(h.CLOCK["now"])
        return result

    hass.async_add_executor_job = executor_with_reports
    source._invalidate_internal_inputs()
    await source._recalculate_and_write()
    assert len(calls) == 1, "equivalent reports rejected the production solver result"
    assert source._attributes["result_current"] is True
    assert source._attributes["recalculation_pending"] is False
    assert source._stale_result_retry_cancel is None

    def probe():
        h.CLOCK["now"] = fixture.NOW
        item = object.__new__(module.HoymilesRCEOptimizerSensor)
        item.hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Warsaw"),
            states={ENTITY: h.FakeState("51.2", reported=fixture.NOW)})
        item._runtime = SimpleNamespace(
            source_device=SimpleNamespace(id="device-a"),
            shared_inputs=SimpleNamespace(snapshot=SimpleNamespace(
                schema_version=1, config_entry_id="entry-a", revision=1,
            )),
        )
        item._entry = SimpleNamespace(entry_id="entry-a", data={})
        item._tariff_plan_source = None
        item._tariff_price_source = None
        item._configured_forecast_source_ids = lambda: frozenset()
        item._input_revision = module.OptimizerInputRevision()
        item._attributes = {"result_current": True, "recalculation_pending": False}
        item._timeline_sensor = None
        item._stale_result_retry_cancel = None
        item.async_write_ha_state = lambda: None
        return item

    checks = []
    for label, value, offset, advance, attrs in (
        ("same_fresh_report", "51.2", 3, 3, {}),
        ("changed_voltage", "51.1", 3, 3, {}),
        ("zero_capability", "0", 3, 3, {}),
        ("unknown", "unknown", 3, 3, {}),
        ("non_finite", "nan", 3, 3, {}),
        ("changed_provenance", "51.2", 3, 3, {"source_entity_id": "sensor.other"}),
        ("changed_quality", "51.2", 3, 3, {"quality": "partial"}),
        ("backwards_report", "51.2", -1, 3, {}),
        ("future_report", "51.2", 20, 3, {}),
        ("captured_sample_expired", "51.2", 301, 301, {}),
        ("unchanged_report_expired", "51.2", 0, 301, {}),
    ):
        item = probe()
        captured = item._current_input_fingerprint()
        h.CLOCK["now"] = fixture.NOW + timedelta(seconds=advance)
        item.hass.states[ENTITY] = h.FakeState(
            value, attrs, fixture.NOW + timedelta(seconds=offset),
        )
        rejected = item._reject_stale_executor_result(0, captured)
        # A fresh numeric change may reach the pure feasibility proof; it is
        # never enough by itself to publish the old result or permit a write.
        assert rejected is (label not in {
            "same_fresh_report", "changed_voltage", "zero_capability",
        }), (label, rejected)
        checks.append(label)

    for label, change in (
        ("changed_entry", lambda item: setattr(item._entry, "entry_id", "entry-b")),
        ("changed_source_device", lambda item: item._entry.data.update({module.CONF_SOURCE_DEVICE_ID: "other"})),
        ("changed_resolved_source", lambda item: item._entry.data.update({module.CONF_RESOLVED_SOURCE_DEVICE_ID: "other"})),
        ("changed_runtime_source", lambda item: setattr(item._runtime.source_device, "id", "device-b")),
        ("changed_shared_revision", lambda item: setattr(item._runtime.shared_inputs.snapshot, "revision", 2)),
        ("changed_shared_entry", lambda item: setattr(item._runtime.shared_inputs.snapshot, "config_entry_id", "entry-b")),
        ("missing_source", lambda item: item.hass.states.pop(ENTITY)),
        ("naive_report", lambda item: item.hass.states.update({ENTITY: h.FakeState("51.2", reported=fixture.NOW.replace(tzinfo=None))})),
        ("missing_report_time", lambda item: item.hass.states.update({ENTITY: h.FakeState("51.2", reported=None)})),
        ("immediate_revision", lambda item: item._input_revision.invalidate()),
    ):
        item = probe()
        captured = item._current_input_fingerprint()
        change(item)
        assert item._reject_stale_executor_result(0, captured), label
        checks.append(label)

    await variable_telemetry()
    await invalid_latest_inputs()
    await nonexecuting_results()
    await immutable_provider_maps()
    shared_guard(module)
    await configured_shared_samples(module)
    print(f"PASS RCE publication: six in-flight reports, {len(checks)} guards, 270s variable telemetry, latest-input negative cases")


async def variable_telemetry() -> None:
    h = fixture.h
    h.CLOCK["now"] = fixture.NOW
    hass, _, _, _, source, module, _, _ = await fixture.solver_probe()
    module.numeric_state_sample = h._load(
        "rce_publication_churn_energy", h.COMPONENT / "energy_data.py",
    ).numeric_state_sample
    provider = source._optimizer_input
    load = "sensor.hoymiles_actual_load_power"
    pv = "sensor.hoymiles_hit_overview_pv_total_power"
    sequence = calls = 0
    retained = []
    batches = []

    async def advance(offset):
        nonlocal sequence
        while sequence * 3 <= offset:
            at = fixture.NOW + timedelta(seconds=sequence * 3)
            for entity_id, value in (
                (load, 566 + (sequence * 127) % 2668),
                (pv, (sequence % 3) * 50), (ENTITY, 51.2),
            ):
                hass.states.values[entity_id] = h.FakeState(str(value), reported=at)
            sequence += 1
            h.CLOCK["now"] = at
            await asyncio.sleep(0)
        h.CLOCK["now"] = fixture.NOW + timedelta(seconds=offset)

    def latest_provider():
        settings, metadata = provider()
        return replace(
            settings,
            current_load_power_kw=float(hass.states.values[load].state) / 1000,
            current_pv_power_kw=float(hass.states.values[pv].state) / 1000,
        ), {**metadata, "sample_marker": h.CLOCK["now"].isoformat()}

    source._optimizer_input = latest_provider

    async def interleaved(func, *args):
        nonlocal calls
        calls += 1
        result = func(*args)
        retained.append(result)
        await advance((h.CLOCK["now"] - fixture.NOW).total_seconds() + 4)
        return result

    hass.async_add_executor_job = interleaved
    for start in (1, 121, 241, 266):
        await advance(start)
        source._invalidate_internal_inputs()
        before = calls
        await source._recalculate_and_write()
        # Raw LOAD may trim physical execution, but does not replace the
        # common nominal forecast. Publication must still make progress.
        assert 1 <= calls - before <= 3, (start, calls, source._attributes)
        assert source._attributes["result_current"] is True, (start, source._attributes)
        assert source._attributes["recalculation_pending"] is False
        assert source._stale_result_retry_cancel is None
        batches.append(calls - before)
        assert source._attributes["sample_marker"] == h.CLOCK["now"].isoformat()
        old_slots = {row.start: row.energy_kwh for row in retained[-1].planned_exports}
        assert all(row.energy_kwh <= old_slots[row.start] + 1e-8 for row in source._result.planned_exports)
        modeled = retained[-1]
        assert source._result.current_slot_load_source == 'shared_forecast'
        assert abs(source._result.current_slot_load_kwh - (
            modeled.current_slot_load_kwh / modeled.current_slot_remaining_minutes
            * source._result.current_slot_remaining_minutes
        )) < 1e-9
    assert (h.CLOCK["now"] - fixture.NOW).total_seconds() >= 270
    print(f"PASS variable telemetry: elapsed >=270s, bounded solver attempts {batches}")


async def invalid_latest_inputs() -> None:
    for label in ("bms_zero", "bms_stale", "soc_floor", "soc_stale", "gcf_missing", "market_stale", "source_missing"):
        h = fixture.h
        h.CLOCK["now"] = fixture.NOW
        hass, _, _, _, source, _, _, _ = await fixture.solver_probe()
        provider = source._optimizer_input
        returned = False

        def latest_provider():
            settings, metadata = provider()
            if not returned:
                return settings, metadata
            if label == "bms_zero":
                settings = replace(settings, bms_max_discharge_current_a=0.0, bms_discharge_data_available=False)
            elif label == "bms_stale":
                settings = replace(settings, bms_discharge_data_fresh=False, bms_discharge_data_age_seconds=301.0)
            elif label == "soc_floor":
                settings = replace(settings, battery_soc_percent=0.0)
            elif label == "soc_stale":
                settings = replace(settings, current_battery_soc_fresh=False)
                metadata = {**metadata, "soc_data_fresh": False}
            elif label == "gcf_missing":
                metadata = {**metadata, "gcf_execution_data_fresh": False}
            elif label == "market_stale":
                metadata = {**metadata, "rce_today_data_fresh": False}
            else:
                settings = None
                metadata = {**metadata, "missing_entities": ["source"]}
            return settings, metadata

        async def executor(func, *args):
            nonlocal returned
            result = func(*args)
            returned = True
            await asyncio.sleep(0)
            return result

        source._optimizer_input = latest_provider
        hass.async_add_executor_job = executor
        source._invalidate_internal_inputs()
        await source._recalculate_and_write()
        if source._attributes["result_current"]:
            # A second full solve may publish the new, genuinely blocked
            # state. It must not inherit any authority from the active plan.
            assert label in {"bms_zero", "bms_stale", "soc_floor"}, label
            assert not source._result.planned_exports, label
            assert not source._result.current_slot_start_eligible, label
            assert source._result.current_slot_execution_power_percent == 0.0, label
        else:
            assert source._attributes["recalculation_pending"] is True, label
            assert source._attributes["execution_input_valid"] is False, label
            assert source._attributes["execution_blocker_code"] in {"missing_data", "plan_revalidation_failed"}, label


async def nonexecuting_results() -> None:
    for label in ("disabled", "bms_zero", "zero_export", "empty_market", "bms_zero_churn", "zero_export_churn", "empty_market_churn"):
        h = fixture.h
        h.CLOCK["now"] = fixture.NOW
        hass, _, _, _, source, _, _, _ = await fixture.solver_probe()
        provider = source._optimizer_input
        calls = 0
        if label == "disabled":
            hass.states.values["input_boolean.hoymiles_rce_discharge_enabled"] = h.FakeState("off", reported=fixture.NOW)

        def current_provider():
            settings, metadata = provider()
            if label.startswith("bms_zero"):
                settings = replace(settings, bms_max_discharge_current_a=0.0,
                                   bms_discharge_data_available=False)
            elif label.startswith("zero_export"):
                settings = replace(settings, export_power_cap_kw=0.0)
            elif label.startswith("empty_market"):
                settings = replace(settings, price_slots=[])
            if label.endswith("_churn"):
                settings = replace(settings, current_load_power_kw=0.566 + calls * 0.127,
                                   current_pv_power_kw=calls * 0.05)
            return settings, {**metadata, "automatic_discharge_enabled": label != "disabled"}

        async def executor(func, *args):
            nonlocal calls
            calls += 1
            result = func(*args)
            h.CLOCK["now"] += timedelta(seconds=4)
            await asyncio.sleep(0)
            return result

        source._optimizer_input = current_provider
        hass.async_add_executor_job = executor
        source._invalidate_internal_inputs()
        await source._recalculate_and_write()
        assert calls == 1, (label, calls)
        assert source._attributes["result_current"] is True, (label, source._attributes)
        assert source._attributes["recalculation_pending"] is False, label
        if label == "disabled":
            assert source._attributes["automatic_discharge_enabled"] is False
        else:
            assert not source._result.planned_exports, label
            assert not source._result.current_slot_start_eligible, label
            assert source._result.current_slot_execution_power_percent == 0.0, label


async def immutable_provider_maps() -> None:
    h = fixture.h
    h.CLOCK["now"] = fixture.NOW
    hass, _, _, _, source, _, _, _ = await fixture.solver_probe()
    provider = source._optimizer_input
    owned = {fixture.NOW: 0.0}
    calls = 0

    def latest_provider():
        settings, metadata = provider()
        return replace(settings, pv_by_slot_kwh=owned), metadata

    async def executor(func, *args):
        nonlocal calls
        calls += 1
        worker_map = args[0].pv_by_slot_kwh
        before = dict(worker_map)
        result = func(*args)
        owned[fixture.NOW] += 0.1
        await asyncio.sleep(0)
        assert worker_map == before and worker_map is not owned
        return result

    source._optimizer_input = latest_provider
    hass.async_add_executor_job = executor
    source._invalidate_internal_inputs()
    await source._recalculate_and_write()
    assert calls == 3
    assert source._attributes["result_current"] is False


def shared_guard(module) -> None:
    @dataclass
    class Sample:
        value: float
        reported_at: object
        age_seconds: float = 0.0
        fresh: bool = True
        source_entity_ids: tuple = ("sensor.source_a",)
        quality: str = "complete"

    @dataclass
    class Power:
        home_load_power_kw: Sample

    @dataclass
    class Snapshot:
        power: Power
        schema_version: int = 1
        config_entry_id: str = "entry-a"
        revision: int = 1

    sample = Sample(0.566, fixture.NOW)
    snapshot = Snapshot(Power(sample))
    runtime = SimpleNamespace(shared_inputs=SimpleNamespace(snapshot=snapshot))
    def fingerprint():
        return (("__shared_ems_inputs__", module._shared_optimizer_signature(runtime)),)

    def matches(captured, *, now=fixture.NOW + timedelta(seconds=3)):
        return module._rce_publication_fingerprints_match(captured, fingerprint(), now=now)

    captured = fingerprint()
    sample.value = 3.234
    sample.reported_at += timedelta(seconds=3)
    snapshot.revision += 1
    assert matches(captured)
    for field, changed in (("fresh", False), ("quality", "partial"), ("source_entity_ids", ("sensor.other",))):
        previous = getattr(sample, field)
        setattr(sample, field, changed)
        assert not matches(captured), field
        setattr(sample, field, previous)
    for label, report, now in (
        ("backward", fixture.NOW - timedelta(seconds=1), fixture.NOW + timedelta(seconds=3)),
        ("future", fixture.NOW + timedelta(seconds=20), fixture.NOW + timedelta(seconds=3)),
        ("original_expired", fixture.NOW + timedelta(seconds=121), fixture.NOW + timedelta(seconds=121)),
        ("unchanged_expired", fixture.NOW, fixture.NOW + timedelta(seconds=121)),
        ("missing_clock", None, fixture.NOW + timedelta(seconds=3)),
        ("naive_clock", fixture.NOW.replace(tzinfo=None), fixture.NOW + timedelta(seconds=3)),
    ):
        sample.reported_at = report
        assert not matches(captured, now=now), label


async def configured_shared_samples(module) -> None:
    # Use the real coordinator and _setting_sample contract. Configuration
    # reports may be weeks old while physical telemetry must remain recent.
    import test_ems_shared_inputs as shared_fixture

    # The independent controller fixture preloads a numeric stub in the PV
    # module. Restore its real dependency before exercising the real broker.
    shared_fixture.M.evaluate_pv_forecast_usefulness.__globals__["numeric_state_sample"] = shared_fixture.M.numeric_state_sample
    shared_fixture.M.evaluate_pv_forecast_usefulness.__globals__["state_reported_at"] = shared_fixture.M.state_reported_at

    for old_config in (False, True):
        shared_fixture.CLOCK["now"] = shared_fixture.NOW
        hass, broker = shared_fixture._coordinator()
        hass.states.values[shared_fixture.M.NEW_RATED_POWER_HELPER].state = "12 kW"
        if old_config:
            for entity_id, state in hass.states.values.items():
                if entity_id.startswith(("input_number.", "input_select.")):
                    state.reported = shared_fixture.NOW - timedelta(days=7)
        await broker.async_refresh()
        snapshot = broker.snapshot
        runtime = SimpleNamespace(shared_inputs=SimpleNamespace(snapshot=snapshot))

        def fingerprint():
            return (("__shared_ems_inputs__", module._shared_optimizer_signature(runtime)),)

        captured = fingerprint()
        assert module._rce_publication_fingerprints_match(
            captured, captured, now=shared_fixture.NOW + timedelta(seconds=4),
        ), ("configured_sample_expired", old_config)
        shared_fixture.CLOCK["now"] += timedelta(seconds=3)
        await broker.async_refresh()
        runtime.shared_inputs.snapshot = broker.snapshot
        assert module._rce_publication_fingerprints_match(
            captured, fingerprint(), now=shared_fixture.CLOCK["now"],
        ), "elapsed forecast usefulness age rejected an unchanged real broker refresh"
        for field, changed in (("value", 12.5), ("source_entity_ids", ("input_number.other",)), ("fresh", False)):
            runtime.shared_inputs.snapshot = replace(snapshot, efficiency=replace(
                snapshot.efficiency, pv_to_battery_efficiency=replace(
                    snapshot.efficiency.pv_to_battery_efficiency, **{field: changed},
                ),
            ))
            assert not module._rce_publication_fingerprints_match(
                captured, fingerprint(), now=shared_fixture.NOW + timedelta(seconds=4),
            ), ("changed_configuration", field)

    import test_pv_forecast_usability as pv_fixture

    shared_fixture.CLOCK["now"] = shared_fixture.NOW
    hass, broker = shared_fixture._coordinator()
    hass.config = SimpleNamespace(time_zone="Europe/Warsaw")
    await broker.async_refresh()
    last = shared_fixture.NOW - timedelta(hours=19)
    next_update = shared_fixture.NOW + timedelta(hours=2)
    for key, offset in (("today", 0), ("remaining_today", 0), ("tomorrow", 1), ("day3", 2)):
        sample = getattr(broker.snapshot.forecast, key)
        if sample.entity_id:
            hass.states.values[sample.entity_id] = pv_fixture.state(
                20.0, last, (shared_fixture.NOW + timedelta(days=offset)).date(),
            )
    hass.states.values[pv_fixture.PV.SOLCAST_UPDATE_ENTITY_CANDIDATES[0]] = pv_fixture.update_state(last, next_update)
    await broker.async_refresh()
    assert broker.snapshot.forecast.today.fresh
    assert broker.snapshot.forecast.today.usefulness["mode"] == "scheduled_pause"

    @dataclass
    class ForecastSnapshot:
        forecast: object

    runtime.shared_inputs.snapshot = ForecastSnapshot(broker.snapshot.forecast)
    captured = fingerprint()
    assert module._rce_publication_fingerprints_match(
        captured, captured, now=shared_fixture.NOW + timedelta(seconds=4),
    ), "valid scheduled-pause forecast rejected by raw freshness TTL"
    deadline = module.datetime.fromisoformat(broker.snapshot.forecast.today.usefulness["valid_until"])
    assert not module._rce_publication_fingerprints_match(
        captured, captured, now=deadline + timedelta(seconds=1),
    ), "scheduled-pause publication accepted after its original deadline"
    for key, changed in (("usable", False), ("mode", "expired"), ("coverage_complete", False),
                         ("source_fresh", True), ("target_date", "2099-01-01")):
        forecast = broker.snapshot.forecast
        runtime.shared_inputs.snapshot = ForecastSnapshot(replace(
            forecast, today=replace(forecast.today, usefulness={**forecast.today.usefulness, key: changed}),
        ))
        assert not module._rce_publication_fingerprints_match(
            captured, fingerprint(), now=shared_fixture.NOW + timedelta(seconds=4),
        ), ("changed_forecast_usefulness", key)


if __name__ == "__main__":
    asyncio.run(main())

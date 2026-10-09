"""Cooperative solver/telemetry/YAML/lease regression, 270 virtual seconds.

The HA transport and clock are deterministic fixtures, not a live inverter.
Production optimizer publication, Jinja helper bodies, Supervisor controller and
lease renewal callbacks run unchanged. Solver jobs remain pending for four
seconds while LOAD/PV/BMS reports and physical FC03 observations continue.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from functools import lru_cache
import inspect
import json
from pathlib import Path
import sys

import test_rce_lease_real_cadence as fixture


h = fixture.h
NOW = fixture.NOW
LOAD = "sensor.hoymiles_actual_load_power"
PV = "sensor.hoymiles_hit_overview_pv_total_power"
VOLTAGE = "sensor.hoymiles_hit_battery_voltage_bms"


class Continuity:
    async def setup(self):
        h.CLOCK["now"] = NOW
        (self.hass, self.entry, self.runtime, self.supervisor, self.source,
         self.module, self.renderer, _) = await fixture.solver_probe("natural")
        # Cache compilation only; every render still reads the current states.
        self.renderer.env.from_string = lru_cache(maxsize=64)(self.renderer.env.from_string)
        self.module.numeric_state_sample = h._load(
            "publication_continuity_energy", h.COMPONENT / "energy_data.py"
        ).numeric_state_sample
        self.tasks = []
        self.jobs = []
        self.solver_observations = []
        self.fingerprint_differences = []
        original_match = self.module._rce_publication_fingerprints_match
        def report_match(old, new, now):
            accepted = original_match(old, new, now=now)
            if not accepted:
                differences = [old_item[0] for old_item, new_item in zip(old, new)
                               if not original_match((old_item,), (new_item,), now=now)]
                self.fingerprint_differences.append({"at": self.second, "keys": differences})
                print("Fingerprint rejection", self.second, differences, flush=True)
            return accepted
        self.module._rce_publication_fingerprints_match = report_match
        self.publications = []
        self.telemetry = []
        self.frames = []
        self.generation = self.settings_generation = 1
        self.load_kw = 1.0
        self.pv_kw = 0.0
        basic = {
            "sensor.hoymiles_hit_ems_mode_readback_code": 0,
            "sensor.hoymiles_hit_ems_control_readback_generation": 1,
            "sensor.hoymiles_hit_parallel_topology_readback_generation": 1,
            "sensor.hoymiles_hit_esp_uptime": 1000,
            "sensor.hoymiles_hit_ems_verified_hardware_readback_supported": 1,
            "sensor.hoymiles_hit_overview_battery_soc": 90,
            "sensor.hoymiles_hit_maximum_discharge_current": 500,
            VOLTAGE: 51.2,
            "sensor.hoymiles_hit_ems_force_discharge_soc_readback": 20,
            "sensor.hoymiles_hit_ems_maximum_discharge_power_readback": 50,
            "sensor.hoymiles_hit_gcf_enable_readback_code": 0,
            "sensor.hoymiles_hit_gcf_maximum_export_power_readback": 100,
            "sensor.hoymiles_hit_gcf_control_readback_generation": 1,
            "input_number.hoymiles_rce_requested_discharge_power": 100,
            "input_boolean.hoymiles_rce_dynamic_soc_enabled": "off",
            LOAD: 1000,
            PV: 0,
        }
        for entity_id, value in basic.items():
            self.direct(entity_id, value)
        for key, value in {
            "supervisor_mode": "Active", "allow_rce": "on", "rce_enabled": "on",
            "rce_active": "off", "sale_block_active": "off",
            "rce_price_above_threshold": "on", "rce_latched_minimum_soc": 20,
            "battery_soc": 90, "bms_voltage": 51.2,
            "bms_max_charge_current": 500, "bms_max_discharge_current": 500,
            "ems_mode_readback": 0, "ems_generation": 1,
            "self_use_soc_readback": 20, "backup_soc_readback": 80,
            "charge_soc_readback": 80, "charge_power_ems_readback": 40,
            "discharge_soc_readback": 20, "discharge_power_readback": 50,
            "gcf_enable_readback": 0, "gcf_export_limit_readback": 100,
            "gcf_generation": 1, "machine_type": 0, "inverter_count": 1,
            "topology_generation": 1, "charge_power_readback": 80,
            "battery_charge_generation": 1, "grid_power": 0,
            "battery_power": 0, "load_power": 1000,
        }.items():
            self.direct(self.eid(key), value)
        provider = self.source._optimizer_input

        def changing_provider():
            settings, metadata = provider()
            load = float(self.hass.states.values[LOAD].state) / 1000.0
            pv = float(self.hass.states.values[PV].state) / 1000.0
            return replace(settings, current_load_power_kw=load,
                           current_pv_power_kw=pv,
                           battery_voltage_v=float(self.hass.states.values[VOLTAGE].state),
                           bms_max_discharge_current_a=float(self.hass.states.values[
                               "sensor.hoymiles_hit_maximum_discharge_current"].state)), {
                **metadata, "continuity_sample_at": h.CLOCK["now"].isoformat(),
                "continuity_load_kw": load, "continuity_pv_kw": pv,
            }

        self.source._optimizer_input = changing_provider
        original_publish = self.source.async_write_ha_state

        def publish():
            original_publish()
            self.publications.append({
                "at": self.second, "current": self.source._attributes.get("result_current"),
                "pending": self.source._attributes.get("recalculation_pending"),
                "sample_at": self.source._attributes.get("continuity_sample_at"),
                "load_kw": self.source._attributes.get("continuity_load_kw"),
            })

        self.source.async_write_ha_state = publish
        await self.source._recalculate_and_write()
        assert self.source._attributes["result_current"] is True
        self.hass.states.values[self.eid("rce_latched_slot_end")] = h.FakeState(
            "ignored", {"timestamp": self.source._result.current_slot_end.timestamp()}, NOW
        )
        self.services = fixture.LeaseServices(self.hass, self.entry, self.runtime)
        self.hass.services = self.services
        self.loop = asyncio.get_running_loop()
        self.real_loop_time = self.loop.time
        self.loop.time = lambda: 1000.0 + self.second

        def virtual_call_later(delay, callback):
            def invoke(at):
                result = callback(at)
                if inspect.isawaitable(result):
                    self.spawn(result)
            handle = h.FakeHandle(invoke, h.CLOCK["now"] + timedelta(seconds=delay))
            self.hass.delay_handles.append(handle)
            return handle.cancel

        self.hass.call_later = virtual_call_later
        self.hass.async_create_task = lambda coro, name=None: self.spawn(coro, name)
        self.hass.track_states(tuple(self.module.RCE_EVENT_DRIVEN_ENTITIES),
                               self.source._async_input_changed)
        self.renderer.publish_all(NOW)
        await self.supervisor.add_to_platform_finish()
        self.controller = self.supervisor._controller
        self.controller._clock = lambda: h.CLOCK["now"]
        await self.settle()
        assert self.services.arms == 1, (self.controller.record, self.supervisor._decision_attributes)
        self.original_tx = self.controller.record.transaction.transaction_id
        self.original_deadline = self.controller.record.transaction.deadline
        self.original_hard = self.services.oracle.deadline
        self.hass.async_add_executor_job = self.executor

    @property
    def second(self):
        return (h.CLOCK["now"] - NOW).total_seconds()

    def eid(self, key):
        return h._source_entity_id(h.SENSOR._SOURCE_BY_KEY[key], self.entry.entry_id)

    def direct(self, entity_id, value):
        self.hass.states.values[entity_id] = h.FakeState(str(value), reported=h.CLOCK["now"])

    def report(self, key, value):
        self.hass.fire_report(self.eid(key), h.FakeState(str(value), reported=h.CLOCK["now"]))

    def spawn(self, coro, name=None):
        task = asyncio.create_task(coro, name=name)
        self.tasks.append(task)
        return task

    async def executor(self, func, *args):
        # Compute from the captured immutable arguments; deliberately delay its
        # return while the outer scheduler continues independently.
        result = func(*args)
        future = self.loop.create_future()
        observation = {"started": self.second, "due": self.second + 4,
                       "load_kw": args[0].current_load_power_kw,
                       "pv_kw": args[0].current_pv_power_kw,
                       "reports_at_start": len(self.telemetry)}
        self.solver_observations.append(observation)
        self.jobs.append((self.second + 4, future, result, observation))
        return await future

    async def settle(self):
        for _ in range(30):
            for handle in sorted((item for item in (
                *self.hass.active_delays(), *self.hass.active_points())
                if isinstance(item.when, datetime) and item.when <= h.CLOCK["now"]),
                key=lambda item: item.when):
                handle.run()
            await fixture.drain(self.supervisor)
            await asyncio.sleep(0)
            completed = [task for task in self.tasks if task.done()]
            for task in completed:
                task.result()
                self.tasks.remove(task)
            if not any(isinstance(item.when, datetime) and item.when <= h.CLOCK["now"]
                       for item in (*self.hass.active_delays(), *self.hass.active_points())):
                # Executor jobs may intentionally remain pending across ticks.
                return
        raise AssertionError("scheduler failed to settle")

    def ems(self):
        self.generation += 1
        block = self.services.oracle.active
        assert block is not None
        self.services.oracle.observe(block)
        self.direct("sensor.hoymiles_hit_esp_uptime", 1000 + self.generation)
        for key, value in zip(("ems_mode_readback", "self_use_soc_readback",
                               "backup_soc_readback", "charge_soc_readback",
                               "charge_power_ems_readback", "discharge_soc_readback",
                               "discharge_power_readback"), block):
            self.report(key, int(value) if key == "ems_mode_readback" else value)
        self.report("ems_generation", self.generation)
        for entity_id, value in (
            ("sensor.hoymiles_hit_ems_mode_readback_code", int(block[0])),
            ("sensor.hoymiles_hit_ems_control_readback_generation", self.generation),
            ("sensor.hoymiles_hit_ems_force_discharge_soc_readback", block[5]),
            ("sensor.hoymiles_hit_ems_maximum_discharge_power_readback", block[6]),
        ):
            self.direct(entity_id, value)

    def battery(self):
        seq = int(self.second // 3)
        self.load_kw = 0.7 + (seq % 7) * 0.08
        self.pv_kw = (seq % 3) * 0.05
        # Measurements follow the last physical observation, not command echo.
        discharge_kw = 10.0 * self.services.oracle.physical[6] / 100.0
        for key, value in (
            ("battery_soc", 90), ("bms_voltage", 51.2),
            ("bms_max_charge_current", 500), ("bms_max_discharge_current", 500),
            ("grid_power", (discharge_kw - self.load_kw + self.pv_kw) * 1000),
            ("battery_power", discharge_kw * 1000), ("load_power", self.load_kw * 1000),
        ):
            self.hass.fire_state(self.eid(key), h.FakeState(str(value), reported=h.CLOCK["now"]))
        for entity_id, value in (
            ("sensor.hoymiles_hit_overview_battery_soc", 90),
            ("sensor.hoymiles_hit_maximum_discharge_current", 500),
            (VOLTAGE, 51.2), (LOAD, self.load_kw * 1000), (PV, self.pv_kw * 1000),
        ):
            self.hass.fire_state(entity_id, h.FakeState(str(value), reported=h.CLOCK["now"]))
        self.telemetry.append({"at": self.second, "load_kw": self.load_kw, "pv_kw": self.pv_kw})

    def settings(self):
        self.settings_generation += 1
        for key, value in (
            ("gcf_enable_readback", 0), ("gcf_export_limit_readback", 100),
            ("gcf_generation", self.settings_generation), ("machine_type", 0),
            ("inverter_count", 1), ("topology_generation", self.settings_generation),
            ("charge_power_readback", 80), ("battery_charge_generation", self.settings_generation),
        ):
            self.report(key, value)
        for entity_id in ("sensor.hoymiles_hit_parallel_topology_readback_generation",
                          "sensor.hoymiles_hit_gcf_control_readback_generation"):
            self.direct(entity_id, self.settings_generation)

    async def tick(self, second):
        delta = second - self.second
        h.CLOCK["now"] = NOW + timedelta(seconds=second)
        self.services.oracle.advance(delta)
        if second == 1 or second % 5 == 1:
            self.ems()
        if second == 1 or second % 3 == 0:
            self.battery()
        if second == 1:
            self.hass.fire_state(self.eid("rce_active"), h.FakeState("on", reported=h.CLOCK["now"]))
        if second % 20 == 7:
            self.settings()
        if second in (20, 120, 240):
            self.spawn(self.source._async_timer(h.CLOCK["now"]))
        for job in tuple(self.jobs):
            due, future, result, observation = job
            if due <= second:
                observation.update(completed=second, reports_at_end=len(self.telemetry))
                future.set_result(result)
                self.jobs.remove(job)
        rendered = self.renderer.publish_all(h.CLOCK["now"])
        await self.settle()
        # Publication callbacks can render a fresher helper set in this tick.
        rendered = self.renderer.publish_all(h.CLOCK["now"])
        await self.settle()
        record = self.controller.record
        self.frames.append({"at": second, "state": record.state.value,
                            "oracle": self.services.oracle.state,
                            "result_current": self.source._attributes.get("result_current"),
                            "ready": rendered["hoymiles_rce_control_data_ready"],
                            "renewals": len(self.services.renewals)})
        assert record.state in {h.SENSOR.ActiveState.EXECUTING, h.SENSOR.ActiveState.RETARGETING}, (
            second, record.state, record.reason, self.supervisor._control_lease_gate,
            rendered, self.source._attributes)
        assert self.services.oracle.state in {"confirmed", "pending"}, (
            second, self.services.oracle.state, self.services.renewals,
            self.supervisor._control_lease_gate, self.frames[-30:], self.publications,
            self.solver_observations)
        assert record.transaction.transaction_id == self.original_tx
        assert record.transaction.deadline == self.original_deadline
        assert self.services.oracle.deadline == self.original_hard
        assert not self.services.restore_calls

    async def negative(self, case):
        h.CLOCK["now"] = NOW + timedelta(seconds=271)
        if case == "bms":
            self.report("bms_max_discharge_current", 0)
            self.direct("sensor.hoymiles_hit_maximum_discharge_current", 0)
        elif case == "master_stop":
            self.supervisor.request_master_stop()
        else:
            assert case == "expiry"
            # Stop producing physical reports; source age gates must withdraw
            # renewals and the unchanged firmware TTL must restore authority.
            before = len(self.services.renewals)
            for second in range(271, 391):
                self.services.oracle.advance(1)
                h.CLOCK["now"] = NOW + timedelta(seconds=second)
                self.renderer.publish_all(h.CLOCK["now"])
                await self.settle()
            assert self.services.oracle.state in {"restoring", "disarmed"}
            assert self.supervisor._control_lease_renewal_evidence() is None
            return {"case": case, "final_state": self.controller.record.state.value,
                    "oracle": self.services.oracle.state,
                    "renewals_after_reports_stopped": len(self.services.renewals) - before,
                    "gate": dict(self.supervisor._control_lease_gate)}
        self.renderer.publish_all(h.CLOCK["now"])
        await self.settle()
        h.CLOCK["now"] = NOW + timedelta(seconds=275)
        self.services.oracle.advance(5)
        await self.settle()
        evidence = self.supervisor._control_lease_renewal_evidence()
        assert evidence is None, (case, evidence)
        renewals = len(self.services.renewals)
        h.CLOCK["now"] += timedelta(seconds=6)
        await self.settle()
        assert len(self.services.renewals) == renewals, case
        assert self.controller.record.state not in {h.SENSOR.ActiveState.EXECUTING,
                                                    h.SENSOR.ActiveState.RETARGETING}
        return {"case": case, "final_state": self.controller.record.state.value,
                "gate": dict(self.supervisor._control_lease_gate)}

    async def run(self, negative):
        await self.setup()
        try:
            for second in range(1, 271):
                await self.tick(second)
            assert not self.jobs
            assert len(self.solver_observations) >= 3
            assert all(row["completed"] - row["started"] >= 4
                       and row["reports_at_end"] > row["reports_at_start"]
                       for row in self.solver_observations)
            assert len({row["load_kw"] for row in self.solver_observations}) > 1
            assert len(self.services.renewals) >= 10
            assert all(accepted for _, _, accepted in self.services.renewals)
            assert self.source._attributes["result_current"] is True
            assert self.source._attributes["recalculation_pending"] is False
            assert self.source._stale_result_retry_cancel is None
            assert self.services.arms <= 1 + len(self.solver_observations)
            assert all(row["transaction_id"] == self.original_tx for row in self.services.arm_requests)
            evidence = {
                "duration_seconds": self.second, "telemetry_count": len(self.telemetry),
                "solver_jobs": self.solver_observations, "publications": self.publications,
                "renewals_before_negative": list(self.services.renewals),
                "renewals": self.services.renewals, "arm_requests": self.services.arm_requests,
                "frames": self.frames, "transaction_id": self.original_tx,
                "original_hard_deadline": self.original_hard,
                "negative": await self.negative(negative),
            }
            return evidence
        finally:
            self.loop.time = self.real_loop_time
            for task in self.tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)


async def main():
    results = []
    for case in ("bms", "master_stop", "expiry"):
        probe = Continuity()
        try:
            evidence = await probe.run(case)
        except AssertionError as error:
            if len(sys.argv) > 1:
                Path(sys.argv[1]).write_text(json.dumps({
                    "status": "FAIL", "case": case, "error": str(error),
                    "frames": probe.frames, "publications": probe.publications,
                    "solver_jobs": probe.solver_observations,
                    "fingerprint_differences": probe.fingerprint_differences,
                    "renewals": probe.services.renewals,
                }, indent=2, default=str) + "\n", encoding="utf-8")
            raise
        results.append(evidence)
        print(f"PASS 270s cooperative publication + real helpers/controller/lease, {case}: "
              f"{len(evidence['solver_jobs'])} solver jobs, {len(evidence['renewals'])} renewals")
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps(results, indent=2, default=str) + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())

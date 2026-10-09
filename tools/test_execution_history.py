"""Offline regressions for actual EMS history (no HA or inverter writes)."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("execution_history", ROOT / "custom_components/hoymiles_hit_modbus/execution_history.py")
history = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def evidence():
    return {
        "execution_phase": "executing", "selected_policy": "tariff",
        "selected_action": "tariff_battery_charge", "owner": "tariff",
        "selection_reason": "required_energy_restore", "lifecycle_reason": "executing",
        "transaction_id": "tariff:1", "transaction_evidence_scope": "active",
        "transaction_evidence": {"transaction_id": "tariff:1", "command_sent_at": iso(10),
            "readback_result": "confirmed", "physical_verification": {
                "transaction_id": "tariff:1", "action": "tariff_battery_charge",
                "status": "confirmed", "observed_at": iso(20)}},
    }


class HistoryTests(unittest.TestCase):
    def test_v3_export_status_transition_refreshes_source_frame(self):
        first = evidence()
        first["tariff_decision"] = {
            "source_frame": {"observed_at": iso(10), "input_revision": 1},
            "requested_target_energy_kwh": 5.0,
        }
        first["rce_export_evidence"] = {
            "status": "pending", "reason": "no_net_export",
            "control_authority": False, "observed_at": iso(10),
            "power_w": {"grid": 0},
        }
        recorded = history.supervisor_recorder_projection(first)
        heartbeat = copy.deepcopy(first)
        heartbeat["tariff_decision"]["source_frame"]["input_revision"] = 2
        heartbeat["rce_export_evidence"]["observed_at"] = iso(20)
        heartbeat["rce_export_evidence"]["power_w"]["grid"] = 100
        unchanged = history.supervisor_recorder_projection(
            heartbeat, previous=recorded
        )
        self.assertEqual(unchanged, recorded)

        transition = copy.deepcopy(heartbeat)
        transition["tariff_decision"]["source_frame"]["input_revision"] = 3
        transition["rce_export_evidence"].update(
            status="confirmed", reason="net_export"
        )
        changed = history.supervisor_recorder_projection(
            transition, previous=unchanged
        )
        self.assertEqual(changed["rce_export_evidence"], {
            "status": "confirmed", "reason": "net_export",
            "control_authority": False,
        })
        self.assertEqual(
            changed["tariff_decision"]["source_frame"]["input_revision"], 3
        )

    def test_v3_tariff_source_frame_is_anchored_until_semantic_transition(self):
        first = evidence()
        first["tariff_decision"] = {
            "schema_version": 1,
            "source_frame": {"observed_at": iso(10), "input_revision": 1,
                             "power_cohort_generation": 4},
            "action": "charge", "target_soc_percent": 60.0,
            "requested_target_energy_kwh": 5.0,
            "result_current": True, "recalculation_pending": False,
        }
        first_recorded = history.supervisor_recorder_projection(first)
        self.assertEqual(first_recorded["schema_version"], 3)
        second = copy.deepcopy(first)
        second["tariff_decision"]["source_frame"] = {
            "observed_at": iso(20), "input_revision": 2,
            "power_cohort_generation": 5,
        }
        second_recorded = history.supervisor_recorder_projection(
            second, previous=first_recorded
        )
        self.assertEqual(first_recorded, second_recorded)
        self.assertNotEqual(first["tariff_decision"], second["tariff_decision"])
        second["tariff_decision"]["requested_target_energy_kwh"] = 5.1
        transition = history.supervisor_recorder_projection(
            second, previous=second_recorded
        )
        self.assertEqual(transition["tariff_decision"]["source_frame"],
                         second["tariff_decision"]["source_frame"])
        self.assertEqual(transition["tariff_decision"]["requested_target_energy_kwh"], 5.1)
        self.assertNotEqual(transition, second_recorded)
        self.assertEqual(history.recorded_supervisor_attributes(
            {"recorded_execution": transition}), transition)
        for changed_key, changed_value in (
            ("result_current", False),
            ("recalculation_pending", True),
            ("start_eligible", False),
            ("start_reason", "physical_blocker"),
            ("target_soc_percent", 60.01),
        ):
            changed = copy.deepcopy(first)
            changed["tariff_decision"]["source_frame"]["input_revision"] = 9
            changed["tariff_decision"][changed_key] = changed_value
            projected = history.supervisor_recorder_projection(
                changed, previous=first_recorded
            )
            self.assertNotEqual(projected, first_recorded, changed_key)
            self.assertEqual(projected["tariff_decision"]["source_frame"]["input_revision"], 9)
        commanded = copy.deepcopy(first)
        commanded["tariff_decision"]["source_frame"]["input_revision"] = 10
        commanded["transaction_evidence"]["rollback_status"] = "pending"
        projected_command = history.supervisor_recorder_projection(
            commanded, previous=first_recorded
        )
        self.assertEqual(projected_command["tariff_decision"]["source_frame"]["input_revision"], 10)
        for schema in (1, 2):
            old = copy.deepcopy(first_recorded)
            old["schema_version"] = schema
            self.assertEqual(history.recorded_supervisor_attributes(
                {"recorded_execution": old}), old)
        unknown = copy.deepcopy(first_recorded)
        unknown["schema_version"] = 4
        self.assertEqual(history.recorded_supervisor_attributes(
            {"recorded_execution": unknown}), {})

    def test_missing_and_unit_validation(self):
        for value in (None, "unknown", "unavailable", "nan", "inf", True, ""):
            self.assertIsNone(history.measurement_value("soc", value, {"unit_of_measurement": "%"}))
        for value in (-1, 101):
            self.assertIsNone(history.measurement_value("soc", value, {"unit_of_measurement": "%"}))
        self.assertIsNone(history.measurement_value("pv", -1, {"unit_of_measurement": "W"}))
        self.assertIsNone(history.measurement_value("pv", 1000, {}))

    def test_physical_signs_and_units(self):
        for role in ("battery", "grid"):
            self.assertEqual(history.measurement_value(role, 1200, {"unit_of_measurement": "W"}), -1.2)
            self.assertEqual(history.measurement_value(role, -2, {"unit_of_measurement": "kW"}), 2)
        self.assertEqual(history.measurement_value("pv", 0, {"unit_of_measurement": "W"}), 0)

    def test_unchanged_soc_uses_recorded_state(self):
        bins = history.measurement_bins([(-10, 50, {"unit_of_measurement": "%"}),
                                         (400, 55, {"unit_of_measurement": "%"})], "soc", 0, 900, [(-100, 1000)])
        self.assertEqual([p["value"] for p in bins], [50, 55, 55])
        self.assertEqual(bins[2]["observed_at"], 400)

    def test_time_weighted_power_and_unavailable_gap(self):
        rows = [(0, 1000, {"unit_of_measurement": "W"}), (100, 4000, {"unit_of_measurement": "W"}),
                (300, "unavailable", {}), (600, 0, {"unit_of_measurement": "W"})]
        bins = history.measurement_bins(rows, "pv", 0, 900, [(0, 900)])
        self.assertEqual([p["value"] for p in bins], [3, None, 0])

    def test_restart_and_retention_never_filled(self):
        rows = [(-1, 40, {"unit_of_measurement": "%"}), (600, 45, {"unit_of_measurement": "%"})]
        bins = history.measurement_bins(rows, "soc", 0, 900, [(-10, 300), (350, 900)])
        self.assertEqual([p["value"] for p in bins], [40, None, 45])
        self.assertTrue(all(p["value"] is None for p in history.measurement_bins([], "soc", 0, 900, [(0, 900)])))

    def test_confirmed_requires_matching_active_transaction_and_physical_evidence(self):
        baseline = evidence()
        self.assertTrue(history.decision_evidence("executing", baseline, 30)["confirmed"])
        mutations = [
            lambda a: a.update(transaction_evidence_scope="last"),
            lambda a: a.update(execution_phase="selected"),
            lambda a: a.update(execution_phase="waiting_readback"),
            lambda a: a.update(transaction_id="foreign"),
            lambda a: a.update(owner="foreign"),
            lambda a: a.update(selected_policy="manual"),
            lambda a: a["transaction_evidence"].update(readback_result="pending"),
            lambda a: a["transaction_evidence"].update(command_sent_at=iso(40)),
            lambda a: a["transaction_evidence"]["physical_verification"].update(status="pending"),
            lambda a: a["transaction_evidence"]["physical_verification"].update(transaction_id="foreign"),
            lambda a: a["transaction_evidence"]["physical_verification"].update(action="rce_export"),
            lambda a: a["transaction_evidence"]["physical_verification"].update(observed_at=iso(40)),
            lambda a: a["transaction_evidence"]["physical_verification"].update(observed_at=iso(5)),
        ]
        for mutation in mutations:
            candidate = copy.deepcopy(baseline); mutation(candidate)
            self.assertFalse(history.decision_evidence("executing", candidate, 30)["confirmed"])
        self.assertEqual(baseline, evidence())

    def test_attribute_only_transition_and_last_transaction(self):
        selected = evidence(); selected["execution_phase"] = "selected"
        active = evidence()
        idle = {**active, "execution_phase": "idle", "transaction_evidence_scope": "last", "transaction_id": None}
        events = history.decision_intervals([(30, "same", selected), (40, "same", active),
                                            (50, "same", active), (60, "same", idle)], 0, 900)
        self.assertEqual(len(events), 3)
        self.assertEqual((events[1]["start"], events[1]["end"]), (40, 50))
        self.assertEqual([e["confirmed"] for e in events], [False, True, False])
        self.assertIn("required_energy_restore", events[1]["decision_reasons"])

    def test_gap_restart_and_window_edges(self):
        attrs = evidence()
        events = history.decision_intervals([(30, "executing", attrs), (40, "executing", attrs),
            (70, "executing", attrs), (500, "executing", attrs), (1000, "executing", attrs)],
            0, 900, [(0, 50), (60, 900)])
        self.assertEqual([(e["start"], e["end"]) for e in events], [(30, 40), (70, 70), (500, 500)])
        self.assertEqual(history.decision_intervals([(30, "executing", attrs)], 0, 900, []), [])

    def test_horizon_is_48_elapsed_hours_including_dst(self):
        self.assertEqual(history.HISTORY_SECONDS, 172800)
        self.assertEqual(history.timestamp("2026-10-25T03:00:00+01:00") - history.timestamp("2026-10-23T04:00:00+02:00"), 172800)
        self.assertIsNone(history.timestamp("2026-09-12T10:00:00"))

    def test_event_budget_is_explicit_failure(self):
        with self.assertRaisesRegex(ValueError, "history_event_limit"):
            history.decision_intervals(((n, str(n), {}) for n in range(history.MAX_EVENTS + 1)), 0, 172800)

    def test_executions_ignore_reason_churn_but_stop_at_unconfirmed_or_restart(self):
        a = evidence(); b = evidence(); b['selection_reason'] = 'no_eligible_candidate'
        idle = {**b, 'execution_phase': 'idle'}
        rows = [(30, 'executing', a), (60, 'executing', b), (70, 'idle', idle),
                (80, 'executing', a), (100, 'executing', a), (130, 'executing', a)]
        result = history.execution_intervals(rows, 0, 300, [(0, 110), (120, 300)])
        self.assertEqual([(x['start'], x['end']) for x in result], [(30, 60), (80, 100), (130, 130)])

    def test_grid_import_export_do_not_cancel_in_hour_bars(self):
        rows = [(0, -1000, {'unit_of_measurement': 'W'}), (150, 2000, {'unit_of_measurement': 'W'})]
        point = history.measurement_bins(rows, 'grid', 0, 300, [(0, 300)])[0]
        self.assertEqual(point['value'], -0.5)
        self.assertAlmostEqual(point['positive_kwh'], 1/24, places=6)
        self.assertAlmostEqual(point['negative_kwh'], 1/12, places=6)

    def test_energy_only_counts_confirmed_time_and_physical_direction(self):
        rows = {'grid': [(0, 2000, {'unit_of_measurement': 'W'}), (1800, -1000, {'unit_of_measurement': 'W'})]}
        events = [{'start': 900, 'end': 2700, 'policy': 'rce', 'action': 'rce_export'}]
        result = history.energy_summary(rows, events, 0, 3600, [(0, 3600)])
        self.assertEqual(result['rce_export']['maximum_kwh'], 0.5)
        self.assertEqual(result['rce_export']['covered_seconds'], 1800)
        self.assertIsNone(result['tariff_charge']['maximum_kwh'])

    def test_tariff_night_exact_and_simultaneous_pv_bounds(self):
        rows = {role: [(0, value, {'unit_of_measurement': 'kW'})]
                for role, value in {'grid': -3, 'battery': -2, 'pv': 0, 'load': 1}.items()}
        events = [{'start': 0, 'end': 3600, 'policy': 'tariff', 'action': 'tariff_grid_support_and_charge'}]
        result = history.energy_summary(rows, events, 0, 3600, [(0, 3600)])
        self.assertEqual(result['tariff_charge']['minimum_kwh'], 2)
        self.assertEqual(result['tariff_charge']['maximum_kwh'], 2)
        self.assertEqual(result['grid_home']['minimum_kwh'], 1)
        rows['pv'] = [(0, 1, {'unit_of_measurement': 'kW'})]
        rows['grid'] = [(0, -2, {'unit_of_measurement': 'kW'})]
        result = history.energy_summary(rows, events, 0, 3600, [(0, 3600)])
        self.assertEqual((result['tariff_charge']['minimum_kwh'], result['tariff_charge']['maximum_kwh']), (1, 2))
        self.assertEqual((result['grid_home']['minimum_kwh'], result['grid_home']['maximum_kwh']), (0, 1))

    def test_energy_missing_sources_restart_and_inconsistent_balance(self):
        rows = {'battery': [(0, 1000, {'unit_of_measurement': 'W'})]}
        events = [{'start': 0, 'end': 900, 'policy': 'rcm', 'action': 'rcm_pre_discharge'}]
        result = history.energy_summary(rows, events, 0, 900, [(0, 300), (400, 900)])
        self.assertAlmostEqual(result['rcm_discharge']['maximum_kwh'], 1/12, places=6)
        self.assertEqual(result['rcm_discharge']['covered_seconds'], 300)
        rows = {role: [(0, value, {'unit_of_measurement': 'kW'})]
                for role, value in {'grid': -1, 'battery': -5, 'pv': 0, 'load': 1}.items()}
        events = [{'start': 0, 'end': 900, 'policy': 'tariff', 'action': 'tariff_battery_charge'}]
        result = history.energy_summary(rows, events, 0, 900, [(0, 900)])
        self.assertIsNone(result['tariff_charge']['maximum_kwh'])
        self.assertEqual(result['tariff_charge']['covered_seconds'], 0)

    def test_balance_is_recorded_status_without_restart_fill(self):
        rows = [(0, 'on', {}), (500, 'off', {}), (700, 'on', {})]
        result = history.active_intervals(rows, 0, 900, [(0, 300), (400, 900)])
        self.assertEqual([(x['start'], x['end']) for x in result], [(0, 300), (700, 900)])


if __name__ == "__main__":
    unittest.main()

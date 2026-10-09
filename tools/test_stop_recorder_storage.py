"""STOP history lossless storage regression; no live HA/device writes."""
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('history', ROOT / 'custom_components/hoymiles_hit_modbus/execution_history.py')
history = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)


def frames():
    return [{
        'transaction_id': f'tariff:2026-09-30:{i}', 'at': f'2026-09-30T0{i}:00:00+00:00',
        'reason': 'required_energy_restore', 'deadline': '2026-09-30T08:00:00+00:00',
        'lease_identity': ['tariff', i, '2026-09-30'], 'command_sent_at': None,
        'readback_result': 'unknown' if i % 2 else 'confirmed', 'plan_revision': i,
        'result_current': bool(i % 2), 'recalculation_pending': not bool(i % 2),
        'plan_status': 'ready', 'candidate_blocker': None, 'candidate_reason': 'charge',
        'continuation_eligible': True, 'local_hard_stop': False,
        'source_suppression_reason': None, 'requested_power_kw': 2.3, 'soc': 43.0,
        'protected_floor': 20.0, 'critical_bms_ready': True, 'discharge_direction_ready': True,
        'export_state': 'blocked', 'gcf_enable_code': 1, 'export_limit_percent': 0,
        'physical_mode': 'self_use', 'full_block_generation': 600 + i,
        'bms_max_discharge_current_a': 12.5, 'bms_voltage_v': 52.5,
        'rce_hold_started_at': None, 'rce_hold_budget_seconds': None,
        'retarget_check': {'status': 'rejected', 'reason': 'retarget_not_authorized'},
        'retarget_resume_check': {},
        'export_evidence': {'status': 'unknown', 'reason': 'flow_cohort_missing',
                            'control_authority': False, 'label': 'Zażółć gęślą'},
    } for i in range(8)]


class StopStorageTests(unittest.TestCase):
    def test_stop_payload_reduces_bytes_without_losing_proof(self):
        original = frames()
        # Base intentionally records the full list: RED is a size assertion,
        # not an import error or missing helper.
        encode = getattr(history, 'stop_recorder_projection', lambda value: value)
        compact = encode(original)
        raw_bytes = len(json.dumps(original, ensure_ascii=False).encode())
        compact_bytes = len(json.dumps(compact, ensure_ascii=False).encode())
        self.assertLess(compact_bytes, raw_bytes * .5)
        self.assertEqual(original, frames())
        self.assertEqual(history.recorded_stop_decisions({'recorded_stop_decisions': compact}), original)

    def test_legacy_empty_missing_corrupt_unknown_do_not_fabricate_proof(self):
        self.assertEqual(history.recorded_stop_decisions({'recent_stop_decisions': frames()}), frames())
        self.assertIsNone(history.recorded_stop_decisions({}))
        encoded = history.stop_recorder_projection([])
        self.assertEqual(history.recorded_stop_decisions({'recorded_stop_decisions': encoded}), [])
        valid = history.stop_recorder_projection(frames())
        for key, value in [('schema_version', True), ('schema_version', 99), ('sha256', '0'*64),
                           ('data', '!not-base64'), ('raw_bytes', True), ('encoding', 'unknown')]:
            changed = {**valid, key: value}
            self.assertIsNone(history.recorded_stop_decisions({
                'recorded_stop_decisions': changed, 'recent_stop_decisions': frames()}))

    def test_immutable_snapshot_and_all_sequential_events_beyond_live_eight(self):
        original = frames()
        encoded = history.stop_recorder_projection(original)
        original[0]['reason'] = 'MUTATED'
        self.assertEqual(history.recorded_stop_decisions({'recorded_stop_decisions': encoded}), frames())
        observed = set()
        rolling = []
        for i in range(30):
            event = {**frames()[0], 'transaction_id': str(i)}
            rolling = (rolling + [event])[-8:]
            payload = json.loads(json.dumps(history.stop_recorder_projection(rolling)))
            observed.update(f['transaction_id'] for f in history.recorded_stop_decisions({'recorded_stop_decisions': payload}))
        self.assertEqual(observed, {str(i) for i in range(30)})

    def test_compressor_failure_preserves_raw_immutable_evidence(self):
        source = frames()
        with patch.object(history, '_encode_stop_bytes', side_effect=zlib.error('test')):
            encoded = history.stop_recorder_projection(source)
        self.assertEqual(encoded['encoding'], 'json')
        source[0]['reason'] = 'changed'
        self.assertEqual(history.recorded_stop_decisions({'recorded_stop_decisions': encoded}), frames())

    def test_decoder_rejects_trailing_truncated_and_expansion_bomb(self):
        valid = history.stop_recorder_projection(frames())
        compressed = base64.b64decode(valid['data'])
        for blob in (compressed + b'junk', compressed[:-2], zlib.compress(b' ' * 1_000_000)):
            bad = {**valid, 'data': base64.b64encode(blob).decode()}
            self.assertIsNone(history.recorded_stop_decisions({'recorded_stop_decisions': bad}))


if __name__ == '__main__':
    unittest.main()

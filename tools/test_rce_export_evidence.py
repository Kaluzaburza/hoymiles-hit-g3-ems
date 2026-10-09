"""Command confirmation cannot be mistaken for a net sale."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'custom_components/hoymiles_hit_modbus'))
from supervisor_active_bridge import rce_export_evidence
from supervisor_runtime import ExecutionSourceSnapshot

now = datetime(2026, 9, 28, 18, 6, tzinfo=timezone.utc)
source = ExecutionSourceSnapshot(grid_power_w=0, battery_power_w=599,
    pv_power_w=0, load_power_w=599, grid_power_observed_at=now,
    battery_power_observed_at=now, pv_power_observed_at=now,
    load_power_observed_at=now, power_cohort_complete=True)
assert rce_export_evidence(source, now=now)['reason'] == 'no_net_export'
assert rce_export_evidence(replace(source, grid_power_w=300, load_power_w=299), now=now)['status'] == 'confirmed'
assert rce_export_evidence(replace(source, grid_power_w=900), now=now)['reason'] == 'incoherent_flow_cohort'
assert rce_export_evidence(replace(source, grid_power_observed_at=now-timedelta(seconds=200)), now=now)['status'] == 'pending'
assert rce_export_evidence(replace(source, grid_power_w=float('nan')), now=now)['status'] == 'pending'
assert rce_export_evidence(source, now=now)['control_authority'] is False
from execution_history import supervisor_recorder_projection, SUPERVISOR_UNRECORDED_ATTRIBUTES
assert 'rce_export_evidence' in SUPERVISOR_UNRECORDED_ATTRIBUTES
compact = supervisor_recorder_projection({'rce_export_evidence': rce_export_evidence(source, now=now)})
assert compact['rce_export_evidence'] == {'status': 'pending', 'reason': 'no_net_export', 'control_authority': False}
print('PASS RCE net export evidence is separate from command authority')

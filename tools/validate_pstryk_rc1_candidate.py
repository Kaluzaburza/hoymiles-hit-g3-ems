"""Strict offline artifact contract, separate from historical release freeze.

Usage: python tools/validate_pstryk_rc1_candidate.py --manifest /path/MANIFEST.json
This confirms the exact local bytes and feature gates, never host acceptance.
It does not bypass or change tools/validate_release.py.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
BASE='9ea98786c4a557fa412baa4ae501018ff64fe298'

def git(*args):
    return subprocess.check_output(['git','-c','safe.directory='+ROOT.as_posix(),*args],cwd=ROOT,text=True).strip()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',type=Path,required=True)
    args=p.parse_args();m=json.loads(args.manifest.read_text(encoding='utf-8'))
    assert m['base_sha']==BASE==git('rev-parse','HEAD'),'Exact unified Recorder/control base differs'
    paths=set(git('diff','--name-only','HEAD').splitlines())|set(git('ls-files','--others','--exclude-standard').splitlines())
    expected={f['path'] for f in m['files']}
    assert len(expected)==len(m['files']),'Duplicate manifest paths'
    assert paths==expected,('Unreviewed or missing paths',sorted(paths^expected))
    assert 'AGENTS.md' not in paths
    assert m['host_deployment'] is False and m['pv_execution_accepted'] is False
    assert m.get('ev_manual_power_required') is True
    assert m.get('frontend_revision') == 100
    assert m.get('pv_execution_enabled') is True and m.get('user_activation_requested') is True
    assert 'FRONTEND_ASSET_REVISION = 100' in (ROOT/'custom_components/hoymiles_hit_modbus/assets.py').read_text()
    assert 'const frontendRevision = 100' in (ROOT/'home_assistant/www/hoymiles-dashboard-strategy.js').read_text()
    for f in m['files']:
        path=(ROOT/f['path']).resolve()
        assert path.is_relative_to(ROOT.resolve()),'Manifest path escapes worktree'
        assert hashlib.sha256(path.read_bytes()).hexdigest()==f['sha256'],f['path']
    module=ast.parse((ROOT/'custom_components/hoymiles_hit_modbus/pv_charge_delay.py').read_text(encoding='utf-8'))
    gate=[n.value for n in module.body if isinstance(n,ast.Assign)
          and any(isinstance(t,ast.Name) and t.id=='EXECUTION_ACCEPTED' for t in n.targets)]
    # This activation candidate enables the real user opt-in. Physical field
    # acceptance remains separately false until evidenced on each host.
    assert not gate,'Do not turn acceptance evidence into a global control switch'
    required={'test_pstryk_settling.py','test_pstryk_export_settling.py','test_pv_charge_delay_settling.py',
        'test_pv_charge_delay_control.py','test_rce_settling_extended.py','test_supervisor_sensor_contract.py',
        'test_tariff_pending_dispatch_race.py','test_stop_recorder_storage.py','test_stop_recorder_sqlite.py',
        'test_firmware_readback_contract.py','test_esphome_entry_points.py','test_timeline_platform_registration.py'}
    required.update({'test_ev_load_filter.py', 'test_ev_load_runtime.py', 'test_ev_load_ui.js',
        'test_load_model_freshness_alignment.py', 'test_load_history_recovery.py'})
    required.update({'test_tariff_start_publication_race.py', 'test_tariff_initial_lease_settling.py',
        'test_tariff_event_routing.py', 'test_rce_minimum_net_export.py',
        'test_rce_minimum_input_runtime.py', 'test_rce_price_cache.py', 'test_rce_price_runtime.py',
        'test_supervisor_control_lease.py'})
    results={r['test']:r for r in m['test_results']}
    required.update(m['base_regression_tests'])
    required.update(n for n in results if n.startswith(('test_pstryk_','test_pv_charge_delay_')))
    assert all(results[n]['exit_code']==0 for n in required),'Feature regression not green'
    assert {n for n,r in results.items() if r['exit_code']} == {
        'test_supervisor_helpers_contract.py', 'validate_release.py', 'validate_rce_card.js',
        'test_battery_balancing_contract.py', 'test_battery_balancing_ha_runtime.py',
        'test_rce_run_end_extension.py'}, 'Unexpected or silently removed release HOLD'
    for row in m['test_results']:
        log=(args.manifest.parent/row['log']).resolve()
        assert log.is_relative_to(args.manifest.parent.resolve())
        assert hashlib.sha256(log.read_bytes()).hexdigest()==row['log_sha256'],row['test']
    patch=args.manifest.parent/m['patch_name']
    assert hashlib.sha256(patch.read_bytes()).hexdigest()==m['patch_sha256']
    assert m['firmware']['config']=='PASS' and m['firmware']['compile']=='PASS'
    assert m['firmware']['esphome']=='2026.9.0'
    assert m['recorder_delta_preserved'] is True and m['patch_forward_check']=='PASS' and m['patch_reverse_check']=='PASS'
    for name in ('Pstryk','PV'):
        assert m['recorder'][name]['callbacks']==4320
    assert m['recorder']['PV']['state_rows']<=14
    assert m['recorder']['Pstryk']['state_rows']<=73
    assert m['recorder']['BMS']['callbacks']==19939
    assert m['recorder']['BMS']['state_rows']==19940
    assert m['recorder']['BMS']['database_bytes_after_shutdown']<8*1024*1024
    assert m['recorder']['EV']['callbacks']==4320
    assert m['recorder']['EV']['state_rows']<=2
    assert m['recorder']['EV']['live_fields_not_published'] is True
    assert m['ev_contract']=={'default_enabled':False, 'physical_load_filtered':False,
        'maximum_cache_days':28, 'nominal_power_subtracted':False, 'sensor_required_for_measured_correction':True}
    for name,digest in m['evidence'].items():
        path=(args.manifest.parent/name).resolve()
        assert path.is_relative_to(args.manifest.parent.resolve())
        assert hashlib.sha256(path.read_bytes()).hexdigest()==digest,name
    print('PASS exact integrated offline candidate; deployment, field acceptance and historical freeze remain separate')

if __name__=='__main__':main()

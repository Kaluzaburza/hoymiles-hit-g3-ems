"""Negative controls for the current stable byte/mode/provenance contract."""
from copy import deepcopy
import json
from pathlib import Path
import stable_release_contract as contract


def main():
    manifest = contract.validate()
    raw = (contract.ROOT/contract.CONTRACT_PATH).read_bytes().replace(b'\r\n', b'\n')
    files = {}
    for path, expected in manifest['files'].items():
        data = (contract.ROOT/path).read_bytes()
        if contract.digest(path, data) != expected['sha256']:
            data = data.replace(b'\r\n', b'\n')
        files[path] = (expected['mode'], data)
    def rejected(changed=files, payload=raw, parent=contract.PARENT_SHA, subject=contract.SUBJECT):
        try:
            contract.verify_snapshot(payload, changed, parent=parent, subject=subject)
        except RuntimeError:
            return
        raise AssertionError('Mutation survived the stable gate')
    target = 'custom_components/hoymiles_hit_modbus/supervisor_control_lease.py'
    missing = dict(files); del missing[target]; rejected(missing)
    extra = dict(files); extra['unexpected.py'] = ('100644', b'x'); rejected(extra)
    changed = dict(files); changed[target] = ('100644', files[target][1]+b'\n'); rejected(changed)
    changed = dict(files); changed[target] = ('100755', files[target][1]); rejected(changed)
    changed = dict(files); changed[target] = ('120000', files[target][1]); rejected(changed)
    changed = dict(files); changed[contract.SELF_PATH] = ('100644', files[contract.SELF_PATH][1]+b'\n'); rejected(changed)
    forged = deepcopy(manifest); forged['files'][target]['sha256'] = '0'*64
    rejected(payload=json.dumps(forged).encode())
    rejected(payload=raw+b' ')
    rejected(parent='0'*40)
    rejected(parent=contract.PARENT_SHA+' '+contract.PARENT_SHA)
    rejected(subject='unreviewed candidate')
    # An edited manifest plus an edited runtime is still rejected by the pin.
    rejected(changed=changed, payload=json.dumps(forged).encode())
    contract.verify_runtime(files)
    for path in (target, *contract.VERSION_ONLY_FILES):
        mutation = dict(files)
        mutation[path] = (files[path][0], files[path][1]+b'\n# unauthorized runtime change\n')
        try:
            contract.verify_runtime(mutation)
        except RuntimeError:
            pass
        else:
            raise AssertionError('Runtime logic drift accepted: '+path)
    print('Stable release contract: 12 snapshot and 6 runtime drift mutations rejected; exact candidate PASS')


if __name__ == '__main__':
    main()

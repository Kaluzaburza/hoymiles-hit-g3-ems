"""Exercise the production minimum-input branch with its actual math imports."""
import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1] / 'custom_components/hoymiles_hit_modbus/rce_sensor.py'

def main():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and any(getattr(m, 'name', '') == '_optimizer_input' for m in n.body))
    method = next(n for n in cls.body if getattr(n, 'name', '') == '_optimizer_input')
    start = next(i for i,n in enumerate(method.body) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'minimum_export_entity' for t in n.targets))
    branch = deepcopy(method.body[start:start+3])
    assert isinstance(branch[-1], ast.If)
    imports = [deepcopy(n) for n in tree.body if (isinstance(n, ast.ImportFrom) and n.module == 'math') or (isinstance(n, ast.Import) and any(a.name == 'math' for a in n.names))]
    fn = ast.parse('def probe(self, public_prices=None):\n    pass').body[0]
    fn.body = branch + ast.parse('return minimum_net_export, {}').body
    module = ast.fix_missing_locations(ast.Module(body=imports+[fn], type_ignores=[]))
    namespace = {'_state_number': lambda hass, entity: hass.states.get(entity)}
    exec(compile(module, str(SOURCE), 'exec'), namespace)
    entity = 'input_number.hoymiles_rce_minimum_net_export_power'
    for exists,value,expected in [(False,None,2.0),(True,2.0,2.0),(True,3.0,3.0),(True,0.2,0.2),(True,100.0,100.0),(True,0.19,None),(True,float('nan'),None),(True,float('inf'),None),(True,-1,None),(True,101,None)]:
        states = {entity:value} if exists else {}
        actual,meta = namespace['probe'](SimpleNamespace(hass=SimpleNamespace(states=states)))
        assert actual == expected, (exists,value,actual,expected)
        if expected is None: assert meta['status_code'] == 'missing_data'
        shared, meta = namespace['probe'](SimpleNamespace(hass=SimpleNamespace(states=states)), public_prices=object())
        assert shared == expected, ('Pstryk shared sales minimum', exists, value, shared, expected)
        if expected is None: assert meta['status_code'] == 'missing_data'
        else: assert meta == {}
    print('PASS: shared RCE/Pstryk minimum input; 20 default/configured/boundary/nonfinite/range cases')

if __name__ == '__main__': main()

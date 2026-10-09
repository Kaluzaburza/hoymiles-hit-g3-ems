"""Execute the production HA adapter response contract with a service stub."""
import ast
import asyncio
from collections.abc import Mapping
from enum import Enum, IntEnum
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock


class Family(Enum):
    EMS_COMPLETE_BLOCK = "ems_supervisor_write_complete_block"
    GCF_EXPORT_LIMIT = "ems_supervisor_write_gcf_export_limit"
    BATTERY_CHARGE_LIMIT = "ems_supervisor_write_battery_charge_limit"


class EmsMode(IntEnum):
    SELF_USE = 0
    GRID_CHARGE = 4
    GRID_DISCHARGE = 5


class NotQueued(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


ROOT = Path(__file__).resolve().parents[1]
SENSOR_PATH = (
    ROOT
    / "custom_components"
    / "hoymiles_hit_modbus"
    / "supervisor_sensor.py"
)
tree = ast.parse(SENSOR_PATH.read_text(encoding="utf-8"))
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
           and n.name == "HoymilesSupervisorSensor")
method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "_async_dispatch_atomic_write")
assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "_EMS_PRETRANSPORT_REJECTIONS"
                          for t in n.targets))
reasons = ast.literal_eval(assignment.value.args[0])
namespace = dict(Any=Any, Mapping=Mapping, AtomicWrite=object,
                 AtomicWriteFamily=Family, AtomicWriteNotQueued=NotQueued,
                 EmsMode=EmsMode,
                 _LOGGER=Mock(), _ESPHOME_DOMAIN="esphome",
                 _EMS_PRETRANSPORT_REJECTIONS=reasons)
exec(
    compile(ast.Module(body=[method], type_ignores=[]), str(SENSOR_PATH), "exec"),
    namespace,
)
dispatch = namespace[method.name]
checks = 0


async def scenario(response, expected=None, *, family=Family.EMS_COMPLETE_BLOCK):
    global checks
    calls = []
    async def call(*args, **kwargs):
        calls.append((args, kwargs))
        if isinstance(response, BaseException):
            raise response
        return response
    guard = Mock()
    owner = SimpleNamespace(
        hass=SimpleNamespace(services=SimpleNamespace(async_call=call)),
        _assert_single_transport_instance=guard,
        _esphome_action_service=lambda family: family.value,
        _context="context",
    )
    block = SimpleNamespace(mode=EmsMode.SELF_USE, self_use_soc_percent_4301=30,
        backup_soc_percent_4302=90, force_charge_soc_percent_4303=70,
        maximum_charge_power_percent_4304=50, force_discharge_soc_percent_4305=72,
        maximum_discharge_power_percent_4306=40)
    write = SimpleNamespace(family=family, ems_block=block,
                            snapshot_generation=374, target_percent=75)
    try:
        await dispatch(owner, write)
    except BaseException as err:
        assert expected is not None and type(err) is expected, (response, err)
        if expected is NotQueued:
            assert err.reason == response["reason"]
    else:
        assert expected is None, response
    assert len(calls) == 1 and guard.call_count == 2
    args, kwargs = calls[0]
    assert args[2]["snapshot_generation"] == 374
    assert kwargs["blocking"] is True and kwargs["context"] == "context"
    if family is Family.EMS_COMPLETE_BLOCK:
        assert kwargs["return_response"] is True and len(args[2]) == 8
        assert args[2]["force_discharge_soc"] == 72 and args[2]["maximum_discharge_power"] == 40
    else:
        assert "return_response" not in kwargs and len(args[2]) == 2
    checks += 1


async def main():
    await scenario({"schema_version": 1, "accepted": True, "reason": "accepted"})
    for reason in sorted(reasons):
        await scenario({"schema_version": 1, "accepted": False, "reason": reason}, NotQueued)
    for response in (None, {}, [], True, "accepted",
                     {"schema_version": True, "accepted": True, "reason": "accepted"},
                     {"schema_version": 2, "accepted": True, "reason": "accepted"},
                     {"schema_version": 1, "accepted": 1, "reason": "accepted"},
                     {"schema_version": 1, "accepted": None, "reason": "unknown"},
                     {"schema_version": 1, "accepted": False, "reason": "unknown"},
                     {"schema_version": 1, "accepted": False, "reason": "accepted"},
                     {"schema_version": 1, "accepted": False, "reason": []},
                     {"schema_version": 1, "accepted": True, "reason": "stale_snapshot_generation"}):
        await scenario(response, RuntimeError)
    await scenario(TimeoutError("no response after possible transport"), TimeoutError)
    await scenario(ConnectionError("disconnected after possible transport"), ConnectionError)
    await scenario(None, family=Family.GCF_EXPORT_LIMIT)
    await scenario(None, family=Family.BATTERY_CHARGE_LIMIT)


asyncio.run(main())
print(f"PASS {checks} adapter cases: exact native response; unknown never proves non-dispatch; full payload unchanged")

"""Use the exporter's pure privacy contract without importing Home Assistant."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


_SOURCE = (Path(__file__).resolve().parents[2] / "custom_components" /
           "hoymiles_hit_modbus" / "diagnostic_redaction.py")
_SPEC = spec_from_file_location("_hoymiles_diagnostic_privacy", _SOURCE)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("Cannot load the diagnostic privacy contract")
_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
sanitize_diagnostic_value = _MODULE.sanitize_diagnostic_value

"""Verify that Settings declared defaults match canonical _PARAM_DEFAULTS."""
import json
import sys

from config import Settings
from config_helpers import _PARAM_DEFAULTS


def _default_value(field):
    if hasattr(field, "default"):
        v = field.default
        # Pydantic v2 uses PydanticUndefined sentinel for required fields.
        if type(v).__name__ == "PydanticUndefinedType":
            return None
        return v
    return None


mismatches = {}
for name, field in Settings.model_fields.items():
    if name not in _PARAM_DEFAULTS:
        continue
    expected = _PARAM_DEFAULTS[name]
    actual = _default_value(field)
    if actual != expected:
        mismatches[name] = {"expected": expected, "actual": actual}

if mismatches:
    print("Mismatches between Settings.model_fields and _PARAM_DEFAULTS:")
    print(json.dumps(mismatches, indent=2, default=str))
    sys.exit(1)

print("OK: Settings.model_fields defaults match _PARAM_DEFAULTS.")

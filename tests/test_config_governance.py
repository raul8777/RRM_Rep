"""Config governance tests: ensure no critical default mismatches and endpoints work."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT))


def _run_audit():
    """Run tmp_audit_config.py and return parsed summary."""
    result = subprocess.run(
        [sys.executable, "tmp_audit_config.py"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    # The script writes audit_getattr.txt; read it back.
    audit_path = REPO_ROOT / "audit_getattr.txt"
    if not audit_path.exists():
        raise RuntimeError(f"audit_getattr.txt not found: {result.stderr}")
    with audit_path.open() as f:
        lines = f.readlines()
    total = 0
    critical = 0
    for line in lines:
        if line.startswith("Total:"):
            total = int(line.split(":")[1].strip().split()[0])
        if line.startswith("Critical mismatches:"):
            critical = int(line.split(":")[1].strip())
    return {"total": total, "critical": critical, "lines": lines}


def test_audit_has_zero_critical_mismatches():
    """F1: Critical config defaults must match canonical _PARAM_DEFAULTS."""
    summary = _run_audit()
    assert summary["critical"] == 0, (
        f"Found {summary['critical']} critical getattr default mismatches. "
        "See audit_getattr.txt for details."
    )


def test_get_param_returns_canonical_default_for_missing_attr():
    """F2: get_param must return canonical default when attribute missing."""
    from config_helpers import get_param, _PARAM_DEFAULTS

    class _Dummy:
        pass

    dummy = _Dummy()
    for key, expected in list(_PARAM_DEFAULTS.items())[:5]:
        assert get_param(dummy, key) == expected


def test_runtime_config_matches_baseline():
    """F3: Runtime settings must match canonical defaults."""
    result = subprocess.run(
        [sys.executable, "tmp_verify_runtime_config.py"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Runtime config mismatch:\n{result.stdout}\n{result.stderr}"
    )


def test_validate_config_reports_no_errors_on_canonical_defaults():
    """F3b: validate_config should accept canonical defaults without errors."""
    from config import Settings
    from config_helpers import validate_config

    validation = validate_config(Settings())
    assert validation["ok"] is True
    errors = [issue for issue in validation["issues"] if issue["severity"] == "error"]
    assert errors == []


def test_runtime_restore_normalizes_legacy_futures_category_keys_and_keeps_new_defaults():
    """Legacy MIDCAP DB payloads must restore into MIDCAP_ALT without dropping MEME."""
    from config import Settings
    from config_helpers import load_runtime_overrides_from_db

    class _DB:
        def execute_query(self, sql, params=None):
            if params == ('kelly_config_override',):
                return []
            if params == ('category_thresholds_override',):
                return []
            if params == ('futures_category_thresholds_override',):
                return [{
                    "value": {
                        "MIDCAP": {"stop_loss_pct": 9.9},
                        "LARGE_CAP": {"take_profit_pct": 8.8},
                    }
                }]
            return []

    settings = Settings()
    result = load_runtime_overrides_from_db(settings, _DB())

    assert result["ok"] is True
    assert "futures_category_thresholds" in result["applied_thresholds"]
    assert "MIDCAP" not in settings.futures_category_thresholds
    assert settings.futures_category_thresholds["MIDCAP_ALT"]["stop_loss_pct"] == pytest.approx(9.9)
    assert settings.futures_category_thresholds["LARGE_CAP"]["take_profit_pct"] == pytest.approx(8.8)
    assert "MEME" in settings.futures_category_thresholds


def _api_headers():
    """Build headers for protected endpoints."""
    _headers = {"Content-Type": "application/json"}
    _key = os.environ.get("DASHBOARD_API_KEY")
    if _key:
        _headers["X-API-Key"] = _key
    return _headers


def _api_request(path, method="GET", body=None, timeout=30):
    """Call a local app endpoint from inside the container."""
    import urllib.request
    _url = f"http://localhost:8000{path}"
    _data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(_url, data=_data, headers=_api_headers(), method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


@pytest.mark.skipif(os.environ.get("DASHBOARD_API_KEY") is None, reason="no API key")
def test_drift_check_returns_zero_when_aligned():
    """F4: Drift check must report zero drift when runtime is aligned."""
    data = _api_request("/config/drift-check", method="POST", body={})
    assert data["ok"] is True
    assert data["drift_count"] == 0


@pytest.mark.skipif(os.environ.get("DASHBOARD_API_KEY") is None, reason="no API key")
def test_drift_alert_persisted_and_acknowledged():
    """F5: Drift check persists alerts; acknowledge endpoint marks them."""
    # Introduce a temporary drift on a safe parameter not governed by DB overrides.
    # stop_loss_pct is excluded (governed by category_thresholds).
    # sl_confirmation_candles is in _PARAM_DEFAULTS and not in _EXCLUDED.
    _CANARY_PARAM = "sl_confirmation_candles"
    _CANARY_DRIFT = 99
    original = _api_request("/config/verify", method="POST", body={})
    _api_request("/config/apply", method="POST", body={_CANARY_PARAM: _CANARY_DRIFT})
    try:
        drift = _api_request("/config/drift-check", method="POST", body={})
        assert drift["drift_count"] >= 1
        # new_alerts may be 0 if an identical unacknowledged alert already exists in the last 24h
        # Find an unacknowledged alert
        alerts = _api_request("/config/drift-alerts?acknowledged=false&limit=10")
        assert alerts["ok"] is True
        assert any(a["param"] == _CANARY_PARAM for a in alerts["alerts"])
        alert_id = next(a["id"] for a in alerts["alerts"] if a["param"] == _CANARY_PARAM)
        ack = _api_request(f"/config/drift-alerts/{alert_id}/acknowledge", method="POST", body={"notes": "test"})
        assert ack["ok"] is True
    finally:
        # Restore baseline to keep the runtime aligned
        import config_helpers
        _api_request("/config/apply", method="POST", body={_CANARY_PARAM: config_helpers._PARAM_DEFAULTS[_CANARY_PARAM]})
        # Verify alignment
        verify = _api_request("/config/verify", method="POST", body={})
        assert verify["ok"] is True
        assert verify["mismatch_count"] == 0

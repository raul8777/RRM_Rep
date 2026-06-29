"""Config governance tests: ensure no critical default mismatches and endpoints work."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, "/app")


def _run_audit():
    """Run tmp_audit_config.py and return parsed summary."""
    result = subprocess.run(
        [sys.executable, "tmp_audit_config.py"],
        cwd="/app",
        capture_output=True,
        text=True,
    )
    # The script writes audit_getattr.txt; read it back.
    audit_path = "/app/audit_getattr.txt"
    if not os.path.exists(audit_path):
        raise RuntimeError(f"audit_getattr.txt not found: {result.stderr}")
    with open(audit_path) as f:
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
        cwd="/app",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Runtime config mismatch:\n{result.stdout}\n{result.stderr}"
    )


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
    # Introduce a temporary drift on a safe parameter
    original = _api_request("/config/verify", method="POST", body={})
    _api_request("/config/apply", method="POST", body={"stop_loss_pct": 9.99})
    try:
        drift = _api_request("/config/drift-check", method="POST", body={})
        assert drift["drift_count"] >= 1
        assert drift["new_alerts"] >= 1
        # Find an unacknowledged alert
        alerts = _api_request("/config/drift-alerts?acknowledged=false&limit=10")
        assert alerts["ok"] is True
        assert any(a["param"] == "stop_loss_pct" for a in alerts["alerts"])
        alert_id = next(a["id"] for a in alerts["alerts"] if a["param"] == "stop_loss_pct")
        ack = _api_request(f"/config/drift-alerts/{alert_id}/acknowledge", method="POST", body={"notes": "test"})
        assert ack["ok"] is True
    finally:
        # Restore baseline to keep the runtime aligned
        import config_helpers
        _api_request("/config/apply", method="POST", body={"stop_loss_pct": config_helpers._PARAM_DEFAULTS['stop_loss_pct']})
        # Verify alignment
        verify = _api_request("/config/verify", method="POST", body={})
        assert verify["ok"] is True
        assert verify["mismatch_count"] == 0

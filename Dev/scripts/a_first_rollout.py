#!/usr/bin/env python3
"""A-first rollout runner (A priority, gated degrade/recover).

Implements an operational flow aligned to the plan:
- Start from Profile A on all regimes (priority objective).
- Evaluate gates using Dev/phase4_gate_runner.py.
- Degrade to B/C only when gates require it.
- Attempt return to A as soon as gates recover.

This script does not change trading logic; it only uses existing profile endpoints.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_BASE_URL = os.environ.get("A_FIRST_BASE_URL", "http://localhost:8000")
DEFAULT_API_KEY = os.environ.get(
    "A_FIRST_API_KEY",
    os.environ.get("DASHBOARD_API_KEY", ""),
)
DEFAULT_RUN_IDS = [29600, 48412, 51631, 61556]
REGIMES = ["SPOT_BULL", "RANGE_SCALP", "PRESERVATION", "FUTURES_SHORT"]


BASELINE_A_PARAMS = {
    "kelly_base_usdt": 76.0,
    "kelly_max_usdt": 663.0,
    "kelly_max_pct_liquid": 0.621,
    "futures_kelly_max_usdt": 1162.0,
    "min_signal_strength": 0.359,
    "min_success_probability": 0.527,
    "vol_spike_min": 0.83,
    "stop_loss_pct": 1.03,
    "take_profit_pct": 5.83,
    "futures_sl_pct": 3.33,
    "futures_tp_pct": 7.68,
    "slippage_pct": 0.00057,
}

# Relaxation policy requested by user, still bounded and reversible.
RELAXATION_BANDS = {
    "A": {"spot_sl_mult": 1.20, "futures_sl_mult": 1.20, "spot_tp_mult": 1.08, "futures_tp_mult": 1.08},
    "B": {"spot_sl_mult": 1.12, "futures_sl_mult": 1.12, "spot_tp_mult": 1.05, "futures_tp_mult": 1.05},
    "C": {"spot_sl_mult": 1.08, "futures_sl_mult": 1.08, "spot_tp_mult": 1.03, "futures_tp_mult": 1.03},
}


@dataclass
class PhaseState:
    phase_1_done: bool = False
    phase_2_done: bool = False
    phase_3_done: bool = False
    phase_4_done: bool = False
    phase_5_done: bool = False

    def as_explicit_flags(self) -> Dict[str, str]:
        return {
            "Fase I terminada": "SI" if self.phase_1_done else "NO",
            "Fase II terminada": "SI" if self.phase_2_done else "NO",
            "Fase III terminada": "SI" if self.phase_3_done else "NO",
            "Fase IV terminada": "SI" if self.phase_4_done else "NO",
            "Fase V terminada": "SI" if self.phase_5_done else "NO",
            "Implementacion completa": "SI" if all([
                self.phase_1_done,
                self.phase_2_done,
                self.phase_3_done,
                self.phase_4_done,
                self.phase_5_done,
            ]) else "NO",
        }


class ApiClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = None
        headers = {
            "X-API-Key": self.api_key,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(url, method=method, headers=headers, data=data)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read().decode("utf-8")
                return json.loads(payload) if payload else {"ok": True}
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            return {"ok": False, "error": f"HTTP {e.code}", "detail": text}
        except Exception as e:  # pragma: no cover
            return {"ok": False, "error": str(e)}

    def get_profiles(self) -> Dict[str, Any]:
        return self._request("GET", "/regime/profiles")

    def get_runtime_state(self) -> Dict[str, Any]:
        return self._request("GET", "/system/runtime-state")

    def save_profile(self, regime: str, profile: str, params: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/regime/profiles",
            {
                "regime": regime,
                "profile": profile,
                "params": params,
                "meta": meta,
            },
        )

    def activate_profile(self, regime: str, profile: str, reason: str) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/regime/profiles/activate",
            {
                "regime": regime,
                "profile": profile,
                "reason": reason,
                "apply_now": True,
            },
        )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _round4(v: float) -> float:
    return round(float(v), 4)


def _build_effective_bands(sl_scale: float, tp_scale: float) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    for profile, band in RELAXATION_BANDS.items():
        out[profile] = {
            "spot_sl_mult": float(band["spot_sl_mult"]) * float(sl_scale),
            "futures_sl_mult": float(band["futures_sl_mult"]) * float(sl_scale),
            "spot_tp_mult": float(band["spot_tp_mult"]) * float(tp_scale),
            "futures_tp_mult": float(band["futures_tp_mult"]) * float(tp_scale),
        }
    return out


def _profile_params(label: str, effective_bands: Dict[str, Dict[str, float]]) -> Dict[str, Any]:
    band = effective_bands[label]
    p = dict(BASELINE_A_PARAMS)
    p["stop_loss_pct"] = _round4(BASELINE_A_PARAMS["stop_loss_pct"] * band["spot_sl_mult"])
    p["futures_sl_pct"] = _round4(BASELINE_A_PARAMS["futures_sl_pct"] * band["futures_sl_mult"])
    p["take_profit_pct"] = _round4(BASELINE_A_PARAMS["take_profit_pct"] * band["spot_tp_mult"])
    p["futures_tp_pct"] = _round4(BASELINE_A_PARAMS["futures_tp_pct"] * band["futures_tp_mult"])
    return p


def _active_profiles_map(payload: Dict[str, Any]) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {r: None for r in REGIMES}
    regimes = (payload or {}).get("regimes") or {}
    for regime in REGIMES:
        active = ((regimes.get(regime) or {}).get("active") or {}).get("profile")
        out[regime] = str(active).upper() if active else None
    return out


def _seed_and_activate_profile(
    client: ApiClient,
    profile: str,
    reason: str,
    effective_bands: Dict[str, Dict[str, float]],
) -> Tuple[bool, List[Dict[str, Any]]]:
    params = _profile_params(profile, effective_bands)
    events: List[Dict[str, Any]] = []
    ok_all = True
    for regime in REGIMES:
        save = client.save_profile(
            regime=regime,
            profile=profile,
            params=params,
            meta={
                "strategy": "A-first-gated",
                "created_by": "Dev/scripts/a_first_rollout.py",
                "applied_at": _utc_now_iso(),
                "profile_priority": "A_first",
            },
        )
        events.append({"regime": regime, "action": "save", "profile": profile, "result": save})
        if not bool(save.get("ok")):
            ok_all = False
            continue

        act = client.activate_profile(regime=regime, profile=profile, reason=reason)
        events.append({"regime": regime, "action": "activate", "profile": profile, "result": act})
        if not bool(act.get("ok")):
            ok_all = False

    return ok_all, events


def _run_gate_evaluate(repo_root: Path, report_path: Path, pre_gap_file: Optional[Path], allow_event_aliases: bool) -> Dict[str, Any]:
    _container = os.environ.get("A_FIRST_APP_CONTAINER", "crypto-agent-app")
    _docker_bin = os.environ.get(
        "A_FIRST_DOCKER_BIN",
        r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
    )
    _container_db_url = os.environ.get(
        "A_FIRST_CONTAINER_DB_URL",
        "postgresql://crypto_agent:crypto_agent_password@postgres:5432/crypto_agent",
    )
    _in_container_report = f"/app/Dev/{report_path.name}"

    cmd_container = [
        _docker_bin,
        "exec",
        _container,
        "python",
        "/app/Dev/phase4_gate_runner.py",
        "evaluate",
        "--db-url",
        _container_db_url,
        "--out",
        _in_container_report,
    ]
    if pre_gap_file and pre_gap_file.exists():
        cmd_container.extend(["--pre-gap-file", f"/app/Dev/{pre_gap_file.name}"])
    if allow_event_aliases:
        cmd_container.append("--allow-event-aliases")

    proc: Optional[subprocess.CompletedProcess] = None
    try:
        proc_container = subprocess.run(cmd_container, cwd=str(repo_root), capture_output=True, text=True)
        if proc_container.returncode in (0, 2, 3, 4):
            proc = proc_container
    except Exception:
        proc = None

    # Fallback to host execution only if container path is unavailable.
    if proc is None:
        cmd = [
            sys.executable,
            str(repo_root / "Dev" / "phase4_gate_runner.py"),
            "evaluate",
            "--out",
            str(report_path),
        ]
        if pre_gap_file and pre_gap_file.exists():
            cmd.extend(["--pre-gap-file", str(pre_gap_file)])
        if allow_event_aliases:
            cmd.append("--allow-event-aliases")
        proc = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True)

    summary = {
        "ok": proc.returncode in (0, 2, 3, 4),
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "report_file": str(report_path),
    }

    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            summary["report"] = report
            summary["status"] = report.get("status")
            return summary
        except Exception as e:
            summary["ok"] = False
            summary["error"] = f"Failed to parse report: {e}"
    return summary


def _phase_1_apply_a_seed_bc(client: ApiClient, effective_bands: Dict[str, Dict[str, float]]) -> Dict[str, Any]:
    # Seed B/C first so degrade path is ready.
    ok_b, events_b = _seed_and_activate_profile(client, "B", "seed_for_gated_fallback", effective_bands)
    ok_c, events_c = _seed_and_activate_profile(client, "C", "seed_for_gated_fallback", effective_bands)

    # Enforce A as active objective.
    ok_a, events_a = _seed_and_activate_profile(client, "A", "phase_1_a_first_baseline", effective_bands)

    profiles = client.get_profiles()
    active_map = _active_profiles_map(profiles)
    all_active_a = all(active_map.get(r) == "A" for r in REGIMES)

    phase_ok = bool(ok_a and all_active_a)
    return {
        "ok": phase_ok,
        "active_profiles": active_map,
        "seed_ok": {"B": ok_b, "C": ok_c, "A": ok_a},
        "events": events_b + events_c + events_a,
        "profiles_snapshot": profiles,
    }


def _phase_2_gate_critical_and_degrade_if_needed(
    client: ApiClient,
    gate_report: Dict[str, Any],
    effective_bands: Dict[str, Dict[str, float]],
) -> Dict[str, Any]:
    report = gate_report.get("report") or {}
    g1 = ((report.get("gate_1") or {}).get("passed") is True)
    g2 = ((report.get("gate_2") or {}).get("passed") is True)

    critical_pass = bool(g1 and g2)
    actions: List[Dict[str, Any]] = []

    if not critical_pass:
        ok_b, events = _seed_and_activate_profile(
            client,
            "B",
            "phase_2_fail_critical_degrade_from_A",
            effective_bands,
        )
        actions.extend(events)
        return {
            "ok": ok_b,
            "critical_pass": False,
            "action": "degrade_to_B",
            "events": actions,
        }

    return {
        "ok": True,
        "critical_pass": True,
        "action": "keep_current",
        "events": actions,
    }


def _phase_3_gate_performance_and_transition(
    client: ApiClient,
    gate_report: Dict[str, Any],
    effective_bands: Dict[str, Dict[str, float]],
) -> Dict[str, Any]:
    report = gate_report.get("report") or {}
    status = str(report.get("status") or "")
    g3_class = str(((report.get("gate_3") or {}).get("classification") or "")).upper()

    actions: List[Dict[str, Any]] = []
    if status == "FAIL_PERFORMANCE" or g3_class == "FAIL_PERFORMANCE":
        ok_b, events = _seed_and_activate_profile(
            client,
            "B",
            "phase_3_fail_performance_degrade_from_A",
            effective_bands,
        )
        actions.extend(events)
        return {
            "ok": ok_b,
            "status": status,
            "gate_3_classification": g3_class,
            "action": "degrade_to_B",
            "events": actions,
        }

    # INCONCLUSIVE: keep current and gather sample.
    return {
        "ok": True,
        "status": status,
        "gate_3_classification": g3_class,
        "action": "hold",
        "events": actions,
    }


def _phase_4_return_to_a(
    client: ApiClient,
    gate_report: Dict[str, Any],
    effective_bands: Dict[str, Dict[str, float]],
) -> Dict[str, Any]:
    report = gate_report.get("report") or {}
    g1 = ((report.get("gate_1") or {}).get("passed") is True)
    g2 = ((report.get("gate_2") or {}).get("passed") is True)
    g3_class = str(((report.get("gate_3") or {}).get("classification") or "")).upper()

    eligible = bool(g1 and g2 and g3_class != "FAIL_PERFORMANCE")
    if not eligible:
        return {
            "ok": True,
            "eligible_for_return": False,
            "action": "hold",
            "reason": "gates_not_eligible_for_return_to_A",
        }

    ok_a, events_a = _seed_and_activate_profile(client, "A", "phase_4_forced_return_to_A", effective_bands)
    profiles = client.get_profiles()
    active_map = _active_profiles_map(profiles)
    all_a = all(active_map.get(r) == "A" for r in REGIMES)

    return {
        "ok": bool(ok_a and all_a),
        "eligible_for_return": True,
        "action": "return_to_A",
        "events": events_a,
        "active_profiles": active_map,
    }


def _verify_traceability_runtime(client: ApiClient, gate_report: Dict[str, Any]) -> Dict[str, Any]:
    runtime = client.get_runtime_state()
    state = (runtime or {}).get("state") or {}

    gate2_pass = bool((((gate_report.get("report") or {}).get("gate_2") or {}).get("passed") is True))

    has_effective_snapshot = "effective_runtime_selection_snapshot" in state
    has_transition_state = "regime_transition_state" in state
    has_volatile = "volatile_state" in state

    volatile = state.get("volatile_state") or {}
    has_cb_until = ("circuit_breaker_until" in volatile)
    has_consecutive_failures = ("consecutive_failures" in volatile)

    # Explicit SPOT+FUTURES applicability signals from runtime endpoint.
    has_spot_runtime = "daily_pnl_spot" in state
    has_futures_runtime = "daily_pnl_futures" in state

    transition = state.get("regime_transition_state") or {}
    transition_has_switch_context = all(
        k in transition for k in ("from_mode", "to_mode", "to_profile", "updated_at")
    ) if isinstance(transition, dict) else False

    # Keep closeout strict on critical traceability/restart-safe guarantees.
    passed = bool(
        gate2_pass
        and has_effective_snapshot
        and has_transition_state
        and has_volatile
        and has_cb_until
        and has_consecutive_failures
        and has_spot_runtime
        and has_futures_runtime
    )

    return {
        "ok": passed,
        "gate_2_passed": gate2_pass,
        "runtime_state_ok": bool((runtime or {}).get("ok")),
        "has_effective_runtime_selection_snapshot": has_effective_snapshot,
        "has_regime_transition_state": has_transition_state,
        "has_volatile_state": has_volatile,
        "volatile_has_circuit_breaker_until": has_cb_until,
        "volatile_has_consecutive_failures": has_consecutive_failures,
        "spot_runtime_present": has_spot_runtime,
        "futures_runtime_present": has_futures_runtime,
        "transition_has_switch_context": transition_has_switch_context,
    }


def _phase_5_closeout(phase_state: PhaseState, artifacts: Dict[str, Any], traceability_check: Dict[str, Any]) -> Dict[str, Any]:
    # Closure phase is complete when previous phases were executed and traceability/restart-safe checks pass.
    gate_report_written = bool((artifacts.get("gate_report") or {}).get("report_file"))
    done = bool(
        phase_state.phase_1_done
        and phase_state.phase_2_done
        and phase_state.phase_3_done
        and phase_state.phase_4_done
        and gate_report_written
        and bool(traceability_check.get("ok"))
    )
    return {
        "ok": done,
        "gate_report_written": gate_report_written,
        "traceability_restart_safe": traceability_check,
    }


def _print_explicit_status(state: PhaseState) -> None:
    flags = state.as_explicit_flags()
    for k, v in flags.items():
        print(f"{k}: {v}")


def _run_once(
    repo_root: Path,
    client: ApiClient,
    pre_gap_file: Path,
    allow_event_aliases: bool,
    effective_bands: Dict[str, Dict[str, float]],
    gate_report_file: Path,
) -> Tuple[PhaseState, Dict[str, Any]]:
    state = PhaseState()
    artifacts: Dict[str, Any] = {
        "started_at": _utc_now_iso(),
        "config": {
            "base_url": client.base_url,
            "repo_root": str(repo_root),
            "run_ids": DEFAULT_RUN_IDS,
            "effective_bands": effective_bands,
        },
    }

    p1 = _phase_1_apply_a_seed_bc(client, effective_bands)
    artifacts["phase_1"] = p1
    state.phase_1_done = bool(p1.get("ok"))

    gate_eval = _run_gate_evaluate(repo_root, gate_report_file, pre_gap_file, allow_event_aliases)
    artifacts["gate_report"] = gate_eval

    p2 = _phase_2_gate_critical_and_degrade_if_needed(client, gate_eval, effective_bands)
    artifacts["phase_2"] = p2
    state.phase_2_done = bool(p2.get("ok"))

    p3 = _phase_3_gate_performance_and_transition(client, gate_eval, effective_bands)
    artifacts["phase_3"] = p3
    state.phase_3_done = bool(p3.get("ok"))

    p4 = _phase_4_return_to_a(client, gate_eval, effective_bands)
    artifacts["phase_4"] = p4
    state.phase_4_done = bool(p4.get("ok"))

    traceability_check = _verify_traceability_runtime(client, gate_eval)
    artifacts["traceability_check"] = traceability_check

    p5 = _phase_5_closeout(state, artifacts, traceability_check)
    artifacts["phase_5"] = p5
    state.phase_5_done = bool(p5.get("ok"))

    artifacts["finished_at"] = _utc_now_iso()
    artifacts["explicit_status"] = state.as_explicit_flags()
    return state, artifacts


def main() -> int:
    parser = argparse.ArgumentParser(description="A-first rollout with gates and mandatory return to A")
    parser.add_argument("--repo-root", default=".", help="Path to crypto-agent repo root")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument(
        "--no-event-aliases",
        dest="allow_event_aliases",
        action="store_false",
        help="Disable open/close aliases and require strict order_open/close_reason_normalized events",
    )
    parser.set_defaults(allow_event_aliases=True)
    parser.add_argument("--out", default="Dev/a_first_rollout_report_2026_07_04.json")
    parser.add_argument("--pre-gap-file", default="Dev/phase4_pre_gap.json")
    parser.add_argument("--sl-scale", type=float, default=1.0, help="Global scale for SL multipliers across A/B/C")
    parser.add_argument("--tp-scale", type=float, default=1.0, help="Global scale for TP multipliers across A/B/C")
    parser.add_argument("--loop-minutes", type=float, default=0.0, help="If >0, rerun rollout every N minutes")
    parser.add_argument("--max-runs", type=int, default=1, help="Maximum runs when loop is enabled (0 means unlimited)")
    args = parser.parse_args()

    if not args.api_key:
        print(json.dumps({"ok": False, "error": "Missing API key. Set --api-key or DASHBOARD_API_KEY"}, indent=2))
        return 2

    repo_root = Path(args.repo_root).resolve()
    out_path = (repo_root / args.out).resolve() if not Path(args.out).is_absolute() else Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    client = ApiClient(args.base_url, args.api_key)
    effective_bands = _build_effective_bands(args.sl_scale, args.tp_scale)

    pre_gap = Path(args.pre_gap_file)
    pre_gap_file = (repo_root / pre_gap).resolve() if not pre_gap.is_absolute() else pre_gap
    interval_seconds = max(0.0, float(args.loop_minutes)) * 60.0

    reports: List[Dict[str, Any]] = []
    run_idx = 0
    last_exit = 4

    while True:
        run_idx += 1
        gate_report_file = (repo_root / "Dev" / f"a_first_gate_report_2026_07_04_run{run_idx:02d}.json").resolve()
        state, artifacts = _run_once(
            repo_root=repo_root,
            client=client,
            pre_gap_file=pre_gap_file,
            allow_event_aliases=args.allow_event_aliases,
            effective_bands=effective_bands,
            gate_report_file=gate_report_file,
        )
        reports.append(artifacts)

        explicit = artifacts["explicit_status"]
        print(json.dumps({"ok": True, "run": run_idx, "report": str(out_path)}, indent=2, ensure_ascii=True))
        _print_explicit_status(state)
        last_exit = 0 if explicit["Implementacion completa"] == "SI" else 4

        if interval_seconds <= 0:
            break

        if args.max_runs > 0 and run_idx >= args.max_runs:
            break

        time.sleep(interval_seconds)

    payload = {
        "loop_enabled": interval_seconds > 0,
        "interval_seconds": interval_seconds,
        "max_runs": args.max_runs,
        "total_runs": run_idx,
        "reports": reports,
        "latest_explicit_status": reports[-1]["explicit_status"] if reports else {},
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return last_exit


if __name__ == "__main__":
    raise SystemExit(main())

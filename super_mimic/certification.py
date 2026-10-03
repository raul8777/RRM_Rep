from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Tuple


_STAGE_PARAMETER_PREFIXES = {
    "entry": (
        "min_", "entry_", "staged_", "range_", "max_positions", "contract_",
        "reprice_", "slippage_", "kelly_",
    ),
    "kelly": (
        "kelly_", "futures_kelly_", "max_loss_", "slippage_", "concentration_",
    ),
    "lifecycle": (
        "exit_", "hard_", "trailing_", "breakeven_", "stagnation_", "momentum_",
        "belief_", "drawdown_", "peak_", "profit_", "sr_path_",
    ),
    "aic": ("close_", "fee_", "funding_", "slippage_", "latency_", "fill_"),
    "e2e": ("",),
}


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_parameter_scope(stage: str, grid: Dict[str, List[Any]]) -> Dict[str, Any]:
    stage = stage.lower()
    prefixes = _STAGE_PARAMETER_PREFIXES.get(stage)
    if prefixes is None:
        raise ValueError(f"Unsupported stage: {stage}")
    disallowed = []
    if stage != "e2e":
        disallowed = [key for key in grid if not key.startswith(prefixes)]
    return {
        "stage": stage,
        "valid": not disallowed,
        "varied_parameters": sorted(key for key, values in grid.items() if len(values) > 1),
        "frozen_parameters": sorted(key for key, values in grid.items() if len(values) <= 1),
        "disallowed_parameters": sorted(disallowed),
        "grid_hash": stable_hash(grid),
    }


def _metric_vector(result: Dict[str, Any]) -> Tuple[Any, ...]:
    return tuple(result.get(key) for key in (
        "trades", "pnl_usdt", "pnl_pct", "win_rate", "profit_factor",
        "max_drawdown_pct", "peak_capture_ratio", "capital_deployment_ratio", "entry_funnel",
    ))


def build_differentiation_report(results: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = [row for row in results if isinstance(row, dict)]
    unique_configs = {stable_hash(row.get("params") or {}) for row in rows}
    unique_outputs = {stable_hash(_metric_vector(row)) for row in rows}
    by_parameter: Dict[str, Dict[str, List[Tuple[Any, ...]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        vector = _metric_vector(row)
        for key, value in (row.get("params") or {}).items():
            by_parameter[key][json.dumps(value, sort_keys=True, default=str)].append(vector)

    sensitivity = {}
    active = 0
    testable = 0
    for key, groups in sorted(by_parameter.items()):
        if len(groups) < 2:
            continue
        testable += 1
        group_outputs = {
            stable_hash(sorted(vectors, key=lambda item: str(item)))
            for vectors in groups.values()
        }
        changed = len(group_outputs) > 1
        if changed:
            active += 1
        sensitivity[key] = {
            "values": len(groups),
            "output_changed": changed,
            "group_output_hashes": len(group_outputs),
        }

    ratio = active / testable if testable else 0.0
    return {
        "total_results": len(rows),
        "unique_configs": len(unique_configs),
        "unique_outputs": len(unique_outputs),
        "unique_output_ratio": len(unique_outputs) / len(rows) if rows else 0.0,
        "testable_parameters": testable,
        "active_parameters": active,
        "parameter_sensitivity_ratio": ratio,
        "sensitivity": sensitivity,
        "passed": bool(rows) and (testable == 0 or ratio >= 0.80),
    }


def build_invariant_report(results: Iterable[Dict[str, Any]], stage: str) -> Dict[str, Any]:
    rows = list(results)
    violations = []
    observed = defaultdict(int)
    for index, row in enumerate(rows):
        leverage = row.get("leverage")
        if leverage is not None:
            observed["leverage"] += 1
            if float(leverage) > 2.0:
                violations.append({"index": index, "invariant": "leverage_max_2x", "value": leverage})
        partials = row.get("partial_exits")
        if partials is not None:
            observed["partial_exits"] += 1
            if int(partials) != 0:
                violations.append({"index": index, "invariant": "no_partial_exits", "value": partials})
        selected_side = row.get("selected_side")
        executed_side = row.get("executed_side")
        if selected_side is not None and executed_side is not None:
            observed["side_alignment"] += 1
            if selected_side != executed_side:
                violations.append({"index": index, "invariant": "selected_executed_side", "value": [selected_side, executed_side]})
        funnel = row.get("entry_funnel")
        if isinstance(funnel, dict):
            observed["entry_funnel"] += 1
        e2e = row.get("e2e_evidence")
        if isinstance(e2e, dict):
            observed["e2e_evidence"] += 1
            if e2e.get("entries", 0) > 0 and e2e.get("closes", 0) > 0 and e2e.get("contracts") == e2e.get("entries"):
                observed["e2e_lineage"] += 1
            for violation in e2e.get("violations", []):
                violations.append({"index": index, "invariant": violation, "value": True})
        contract = row.get("kelly_contract")
        if isinstance(contract, dict):
            observed["kelly_contract"] += 1
            for invariant in ("risk_within_cap", "margin_not_above_kelly", "stop_unchanged", "target_unchanged", "valid_geometry"):
                if not contract.get(invariant, False):
                    violations.append({"index": index, "invariant": invariant, "value": contract.get(invariant)})
        expected = row.get("expected_exit_price")
        real = row.get("real_exit_price")
        if expected is not None and real is not None:
            observed["expected_real_exit"] += 1
    required = {
        "entry": ("entry_funnel",),
        "kelly": ("kelly_contract", "leverage"),
        "lifecycle": ("partial_exits",),
        "aic": ("expected_real_exit",),
        "e2e": ("entry_funnel", "e2e_lineage"),
    }.get(stage, ())
    missing = [name for name in required if not observed[name]]
    return {
        "stage": stage,
        "rows": len(rows),
        "observed": dict(observed),
        "violations": violations,
        "missing_required_evidence": missing,
        "passed": bool(rows) and not violations and not missing,
    }


def build_reproducibility_report(results: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    grouped: Dict[str, set] = defaultdict(set)
    for row in results:
        identity = stable_hash({
            "window_id": row.get("window_id"),
            "profile": row.get("profile"),
            "params": row.get("params") or {},
            "seed": row.get("seed"),
        })
        grouped[identity].add(stable_hash(_metric_vector(row)))
    conflicts = [identity for identity, outputs in grouped.items() if len(outputs) > 1]
    return {
        "repeated_job_groups": sum(1 for outputs in grouped.values() if len(outputs) == 1),
        "conflicting_job_groups": len(conflicts),
        "conflict_hashes": conflicts,
        "passed": not conflicts,
    }

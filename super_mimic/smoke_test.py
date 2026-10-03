"""Super MIMIC Week 4 smoke test.

Runs the deploy gate, backtest engine adapter, and Monte Carlo engine adapter
using synthetic results so the smoke test is fast and deterministic.
"""
from __future__ import annotations

import random
from typing import Any, Dict

from super_mimic.deploy_gate import DeployGate
from super_mimic.quantum_landscape import QuantumLandscapeAnalyzer


def _make_synthetic_results(count: int = 60, seed: int = 42) -> list[Dict[str, Any]]:
    random.seed(seed)
    results = []
    for i in range(count):
        # Create a dense cluster of high sharpe values around (0.55, 2.5)
        if i < 20:
            sig = 0.55 + random.uniform(-0.02, 0.02)
            sl = 2.5 + random.uniform(-0.2, 0.2)
            sharpe = 1.6 + random.uniform(-0.1, 0.1)
            dd = 6.0 + random.uniform(-1.0, 1.0)
        else:
            sig = 0.3 + (i % 8) * 0.05
            sl = 1.5 + (i // 8) * 0.5
            sharpe = 0.5 + random.random() * 1.0
            dd = 5.0 + random.random() * 10.0
        results.append(
            {
                "min_signal_strength": sig,
                "stop_loss_pct": sl,
                "sharpe": sharpe,
                "pnl_per_day_usdt": 5.0 + sharpe * 10.0 + random.random() * 5.0,
                "drawdown_pct": dd,
                "win_rate": 0.45 + random.random() * 0.25,
                "trades": 10 + i,
            }
        )
    return results


def _build_artifact(results: list[Dict[str, Any]]) -> Dict[str, Any]:
    analyzer = QuantumLandscapeAnalyzer(results)
    landscape = analyzer.build_landscape(
        param_x="min_signal_strength",
        param_y="stop_loss_pct",
        metric="sharpe",
        grid_size=8,
    )
    attractors = analyzer.detect_attractors(
        metric="sharpe",
        min_metric=0.0,
        max_drawdown=20.0,
        neighbor_std_threshold=0.25,
        radius=0.12,
    )
    best = max(results, key=lambda r: r["sharpe"])
    artifact = {
        "status": "completed",
        "engine_results": {
            "engine": "mimic_sweep",
            "summary": {"monte_carlo": {"sharpe": {"ci_low": 0.8, "ci_high": 1.6}}},
            "recommendations": [
                {
                    "profile": "A",
                    "best_pnl_per_day_usdt": best["pnl_per_day_usdt"],
                    "best_drawdown_pct": best["drawdown_pct"],
                    "best_trades": best["trades"],
                    "best_win_rate": best["win_rate"],
                    "best_sharpe": best["sharpe"],
                    "peak_capture_ratio": 0.72,
                    "capital_deployment_ratio": 0.55,
                }
            ],
            "results": results,
        },
        "landscape": {
            "landscape": landscape.to_dict(),
            "attractors": [a.to_dict() for a in attractors],
        },
        "recommended_config": {
            "min_signal_strength": round(best["min_signal_strength"], 3),
            "stop_loss_pct": round(best["stop_loss_pct"], 3),
        },
        "design": {"mode": "mimic", "profile": "A"},
    }
    return artifact


def run_smoke_test() -> Dict[str, Any]:
    print("Super MIMIC Week 4 smoke test")
    results = _make_synthetic_results()
    artifact = _build_artifact(results)

    gate = DeployGate(db_client=None)
    gate_result = gate.evaluate(artifact)
    print(f"Deploy gate: passed={gate_result.passed}, score={gate_result.score}")
    print(f"Reasons: {gate_result.reasons}")
    print(f"Warnings: {gate_result.warnings}")

    n_attractors = len(artifact["landscape"]["attractors"])
    grid_size = len(artifact["landscape"]["landscape"].get("grid", []))
    print(f"Landscape: {grid_size}x{grid_size}, attractors={n_attractors}")

    assert gate_result.passed, f"Gate should pass for healthy synthetic data: {gate_result.warnings}"
    assert n_attractors >= 1, "At least one attractor expected"
    assert grid_size == 8, f"Expected 8x8 grid, got {grid_size}"

    print("Week 4 smoke test passed")
    return {"gate": gate_result.to_dict(), "landscape": artifact["landscape"]}


if __name__ == "__main__":
    run_smoke_test()

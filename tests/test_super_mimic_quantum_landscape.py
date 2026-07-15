"""Unit tests for super_mimic.quantum_landscape."""
import pytest

from super_mimic.quantum_landscape import QuantumLandscapeAnalyzer


def _make_results():
    """Create a small 2D parameter sweep result set."""
    results = []
    for x in [0.3, 0.4, 0.5, 0.6]:
        for y in [2.0, 2.5, 3.0]:
            # Center of grid is robust and high sharpe
            sharpe = 1.0 - abs(x - 0.45) * 2 - abs(y - 2.5) * 0.3
            if abs(x - 0.45) < 0.05 and abs(y - 2.5) < 0.05:
                sharpe += 0.5
            results.append({
                "min_signal_strength": x,
                "stop_loss_pct": y,
                "sharpe": sharpe,
                "best_sharpe": sharpe,
                "drawdown_pct": 5.0,
                "best_drawdown_pct": 5.0,
            })
    return results


def test_build_landscape_returns_grid():
    results = _make_results()
    analyzer = QuantumLandscapeAnalyzer(results)
    landscape = analyzer.build_landscape(
        param_x="min_signal_strength",
        param_y="stop_loss_pct",
        metric="sharpe",
        grid_size=10,
    )
    assert landscape.param_x == "min_signal_strength"
    assert landscape.param_y == "stop_loss_pct"
    assert len(landscape.grid) > 0
    assert len(landscape.points) == len(results)
    assert landscape.phase_map.get("zones") is not None


def test_detect_attractors_finds_robust_region():
    results = _make_results()
    analyzer = QuantumLandscapeAnalyzer(results)
    attractors = analyzer.detect_attractors(
        metric="sharpe",
        min_metric=0.0,
        max_drawdown=30.0,
        neighbor_std_threshold=1.0,
        radius=1.0,
    )
    assert len(attractors) > 0
    best = max(attractors, key=lambda a: a.mean_metric)
    assert best.mean_metric >= 0.0
    assert best.std_metric <= 1.0
    assert best.max_drawdown <= 30.0


def test_empty_results_return_empty_landscape():
    analyzer = QuantumLandscapeAnalyzer([])
    landscape = analyzer.build_landscape("x", "y", "sharpe", 10)
    assert landscape.grid == []
    assert landscape.points == []


def test_phase_map_counts_all_cells():
    results = _make_results()
    analyzer = QuantumLandscapeAnalyzer(results)
    landscape = analyzer.build_landscape(
        "min_signal_strength", "stop_loss_pct", "sharpe", grid_size=8
    )
    zones = landscape.phase_map["zones"]
    flat = [v for row in landscape.grid for v in row if v is not None]
    total = sum(zones.values())
    assert total == len(flat)

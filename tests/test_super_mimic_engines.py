from typing import Any, Dict
from unittest.mock import patch

import pytest

from super_mimic.core import DesignSpec
from super_mimic.engines import (
    run_backtest_experiment,
    run_mimic_sweep_experiment,
    run_monte_carlo_experiment,
)


def _fake_sweep(design: DesignSpec) -> Dict[str, Any]:
    return {
        "summary": {"pnl": 123.0},
        "recommendations": [
            {
                "profile": design.profile,
                "best_pnl_per_day_usdt": 12.0,
                "best_drawdown_pct": 8.0,
                "best_trades": 10,
                "best_win_rate": 0.55,
                "best_sharpe": 1.2,
                "best_params": {"min_signal_strength": 0.7},
                "best_profile_overrides": {"stop_loss_pct": 2.5},
            }
        ],
        "results": [
            {
                "min_signal_strength": 0.7,
                "stop_loss_pct": 2.5,
                "sharpe": 1.2,
                "pnl_per_day_usdt": 12.0,
                "drawdown_pct": 8.0,
                "win_rate": 0.55,
                "trades": 10,
            }
            for _ in range(10)
        ],
    }


@patch("super_mimic.engines.run_sweep")
def test_run_mimic_sweep_experiment_returns_engine_and_config(mock_sweep):
    mock_sweep.return_value = _fake_sweep(DesignSpec("mimic", "A", engine="mimic_sweep"))
    engine_results, recommended = run_mimic_sweep_experiment(
        DesignSpec("mimic", "A", engine="mimic_sweep")
    )
    assert engine_results.engine == "mimic_sweep"
    assert recommended.get("min_signal_strength") == 0.7
    assert recommended.get("stop_loss_pct") == 2.5


@patch("super_mimic.engines.run_sweep")
def test_run_backtest_experiment_tags_as_backtest(mock_sweep):
    mock_sweep.return_value = _fake_sweep(DesignSpec("mimic", "A", engine="backtest"))
    engine_results, _ = run_backtest_experiment(DesignSpec("mimic", "A", engine="backtest"))
    assert engine_results.engine == "backtest"


@patch("super_mimic.engines.run_sweep")
def test_run_monte_carlo_experiment_adds_bootstrap_ci(mock_sweep):
    mock_sweep.return_value = _fake_sweep(DesignSpec("mimic", "A", engine="monte_carlo"))
    engine_results, _ = run_monte_carlo_experiment(DesignSpec("mimic", "A", engine="monte_carlo"))
    assert engine_results.engine == "monte_carlo"
    assert "monte_carlo" in engine_results.summary
    mc = engine_results.summary["monte_carlo"]
    assert "sharpe" in mc
    assert "ci_low" in mc["sharpe"]
    assert "ci_high" in mc["sharpe"]

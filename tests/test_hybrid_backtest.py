"""Tests for backtest apply, rollback and hybrid optimizer components."""
import json
import pytest
from unittest.mock import MagicMock, patch

from hybrid_optimizer import recommend_config
from hybrid_optimizer.quantum_space import SimulatedQuantumAnnealing
from hybrid_optimizer.signal_swarm import SignalSwarm
from hybrid_optimizer.ml_predictor import LightweightEnsemble, ThompsonBandit
from hybrid_optimizer.capital_allocator import allocate_spot_futures


def test_sqa_optimizer_basic():
    space = {"x": (-5.0, 5.0), "y": (-5.0, 5.0)}

    def evaluator(cfg):
        return -(cfg["x"] ** 2 + cfg["y"] ** 2)

    sqa = SimulatedQuantumAnnealing(space, seed=42)
    result = sqa.optimize(evaluator, population_size=10, iterations=20)
    assert "best_config" in result
    assert "best_score" in result
    # Should approach 0 from negative side
    assert result["best_score"] > -5.0


def test_signal_swarm_consensus():
    swarm = SignalSwarm()
    signal = {"strength": 0.75, "vol_spike": 2.0, "side": "BUY"}
    result = swarm.evaluate_signal("BTCUSDT", signal, "SPOT_BULL", [])
    assert result["consensus"] >= 0.55
    assert result["pass"] is True


def test_capital_allocator():
    alloc = allocate_spot_futures("FUTURES_SHORT", 10, 50)
    assert alloc["futures_pct"] > alloc["spot_pct"]
    assert alloc["spot_pct"] + alloc["futures_pct"] <= 1.0


def test_ml_ensemble_predict():
    ens = LightweightEnsemble()
    cfg = {
        "kelly_max_usdt": 500,
        "futures_kelly_max_usdt": 800,
        "stop_loss_pct": 0.025,
        "take_profit_pct": 0.035,
        "futures_sl_pct": 0.0333,
        "futures_tp_pct": 0.0768,
        "min_signal_strength": 0.55,
    }
    score = ens.predict_pnl(cfg)
    assert isinstance(score, float)


def test_thompson_bandit():
    configs = [{"a": i} for i in range(5)]
    bandit = ThompsonBandit(configs, seed=42)
    arm = bandit.sample()
    assert 0 <= arm < 5
    bandit.update(arm, 10.0)
    assert bandit.arms[arm]["count"] == 1


def test_recommend_config_empty():
    result = recommend_config([], "SPOT_BULL")
    assert result["ok"] is False


def test_recommend_config_shape():
    runs = [
        {"label": "run_1", "params": {
            "kelly_base_usdt": 100, "kelly_max_usdt": 500,
            "kelly_max_pct_liquid": 0.55, "futures_kelly_max_usdt": 800,
            "min_strength": 0.40, "min_prob": 0.50, "vol_spike_min": 1.0,
            "spot_sl_pct": 0.025, "spot_tp_pct": 0.035,
            "futures_sl_pct": 0.0333, "futures_tp_pct": 0.0768,
        }, "pnl_total": 150},
    ]
    result = recommend_config(runs, "SPOT_BULL", spot_pnl_7d=20, futures_pnl_7d=10)
    assert result["ok"] is True
    assert "patch" in result
    assert "capital_allocation" in result
    assert result["safe"] is True


def test_backtest_apply_snapshot(monkeypatch):
    """Smoke test for snapshot helper using mocked DB."""
    mock_db = MagicMock()
    mock_db.execute_update = MagicMock()
    mock_db.execute_query = MagicMock(return_value=[{"value": {}}])
    import app as app_module
    monkeypatch.setattr(app_module, "db_client", mock_db)
    from app import _create_config_snapshot
    from config import Settings
    s = Settings()
    snap_id = _create_config_snapshot(s)
    assert snap_id
    assert mock_db.execute_update.call_count >= 3

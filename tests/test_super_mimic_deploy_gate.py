import pytest
from unittest.mock import patch

from super_mimic.deploy_gate import DeployGate, DeployGateResult


def make_artifact(
    pnl_per_day=12.0,
    drawdown=8.0,
    trades=12,
    attractors=2,
    peak_capture_ratio=0.75,
    capital_deployment_ratio=0.60,
    recommended=None,
):
    return {
        "status": "completed",
        "engine_results": {
            "summary": {},
            "recommendations": [
                {
                    "profile": "A",
                    "best_pnl_per_day_usdt": pnl_per_day,
                    "best_drawdown_pct": drawdown,
                    "best_trades": trades,
                    "best_win_rate": 0.55,
                    "best_sharpe": 1.2,
                    "best_peak_capture_ratio": peak_capture_ratio,
                    "best_cdr": capital_deployment_ratio,
                }
            ],
        },
        "landscape": {
            "landscape": {},
            "attractors": [
                {"mean_metric": 1.0, "std_metric": 0.05, "max_drawdown": 5.0}
                for _ in range(attractors)
            ],
        },
        "recommended_config": recommended or {"min_signal_strength": 0.7, "stop_loss_pct": 2.5},
        "design": {"mode": "mimic", "profile": "A"},
    }


def test_deploy_gate_passes_for_healthy_artifact():
    gate = DeployGate(db_client=None)
    result = gate.evaluate(make_artifact())
    assert isinstance(result, DeployGateResult)
    assert result.passed is True
    assert result.score >= 0.7
    assert any("PnL" in r for r in result.reasons)
    assert all(r["ok"] for r in result.hard_rules.values())


def test_deploy_gate_blocks_negative_pnl():
    gate = DeployGate(db_client=None)
    result = gate.evaluate(make_artifact(pnl_per_day=-5.0))
    assert result.passed is False
    assert any("pnl" in w.lower() for w in result.warnings)


def test_deploy_gate_blocks_high_drawdown():
    gate = DeployGate(db_client=None)
    result = gate.evaluate(make_artifact(drawdown=20.0))
    assert result.passed is False


def test_deploy_gate_blocks_low_trades():
    gate = DeployGate(db_client=None)
    result = gate.evaluate(make_artifact(trades=1))
    assert result.passed is False


def test_deploy_gate_blocks_no_attractors():
    gate = DeployGate(db_client=None)
    result = gate.evaluate(make_artifact(attractors=0))
    assert result.passed is False


def test_deploy_gate_result_serializable():
    gate = DeployGate(db_client=None)
    result = gate.evaluate(make_artifact())
    d = result.to_dict()
    assert d["passed"] is True
    assert "score" in d
    assert "hard_rules" in d


def test_apply_recommended_config_records_deployment():
    calls = {"record": None}

    class FakeDB:
        def execute_query(self, *args, **kwargs):
            return [{"value": '{"kelly_base_usdt": 100}'}]

    class FakeReg:
        def record_deployment(self, **kwargs):
            calls["record"] = kwargs

    gate = DeployGate(db_client=FakeDB())
    with patch("super_mimic.core.ExperimentRegistry") as MockReg:
        MockReg.return_value = FakeReg()
        result = gate.apply_recommended_config(
            experiment_id="exp-123",
            recommended_config={"kelly_max_usdt": 2000},
            operator="test",
            mode="mimic",
            profile="A",
        )
        assert result["ok"] is True
        assert result["recorded"] is True
        assert calls["record"]["experiment_id"] == "exp-123"
        assert calls["record"]["changes"]["kelly_max_usdt"] == 2000

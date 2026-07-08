import pytest
from portfolio_selector import Candidate, select_portfolio, select_for_regime, get_max_k_for_regime


class DummySettings:
    """Minimal settings object for selector tests."""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _settings_with_qubo(**extra):
    defaults = {
        'qubo_lambda_corr': 1.0,
        'qubo_lambda_size': 5.0,
        'qubo_lambda_risk': 0.5,
        'qubo_solver': 'brute',
        'qubo_population_size': 10,
        'qubo_iterations': 10,
        'qubo_seed': 42,
        'max_positions_spot_bull': 5,
        'max_positions_range_scalp': 3,
        'max_positions_preservation': 1,
        'max_positions_futures_short': 5,
        'max_positions': 4,
    }
    defaults.update(extra)
    return DummySettings(**defaults)


def test_select_portfolio_respects_k():
    candidates = [
        Candidate('A', expected_pnl=10.0, success_probability=0.8, risk_score=30.0),
        Candidate('B', expected_pnl=9.0, success_probability=0.7, risk_score=40.0),
        Candidate('C', expected_pnl=8.0, success_probability=0.6, risk_score=50.0),
        Candidate('D', expected_pnl=7.0, success_probability=0.6, risk_score=60.0),
    ]
    corr = {}
    result = select_portfolio(candidates, corr, k=2, solver='brute')
    assert len(result.selected) == 2
    assert len(result.excluded) == 2
    assert result.solver == 'brute'


def test_select_portfolio_prefers_low_correlation():
    # A and B are highly correlated; C and D are independent.
    candidates = [
        Candidate('A', expected_pnl=10.0, success_probability=0.8, risk_score=30.0),
        Candidate('B', expected_pnl=9.5, success_probability=0.8, risk_score=30.0),
        Candidate('C', expected_pnl=8.5, success_probability=0.7, risk_score=35.0),
        Candidate('D', expected_pnl=8.0, success_probability=0.7, risk_score=35.0),
    ]
    corr = {
        ('A', 'B'): 0.95,
        ('B', 'A'): 0.95,
        ('C', 'D'): 0.05,
        ('D', 'C'): 0.05,
    }
    result = select_portfolio(candidates, corr, k=2, solver='brute',
                              lambda_corr=2.0, lambda_size=10.0, lambda_risk=0.1)
    selected_symbols = {c.symbol for c in result.selected}
    # With strong correlation penalty, the optimizer should never select the two
    # highly-correlated assets together.
    assert not ({'A', 'B'} <= selected_symbols)


def test_select_portfolio_filters_empty_candidates():
    result = select_portfolio([], {}, k=2, solver='brute')
    assert result.selected == []
    assert result.excluded == []
    assert result.solver == 'none'


def test_select_for_regime_preservation_returns_empty():
    settings = _settings_with_qubo()
    candidates = [Candidate('A', expected_pnl=10.0, success_probability=0.8, risk_score=30.0)]
    result = select_for_regime(candidates, {}, settings, 'PRESERVATION')
    assert result.selected == []
    assert result.excluded == candidates


def test_get_max_k_for_regime():
    settings = _settings_with_qubo()
    assert get_max_k_for_regime(settings, 'SPOT_BULL') == 5
    assert get_max_k_for_regime(settings, 'RANGE_SCALP') == 3
    assert get_max_k_for_regime(settings, 'FUTURES_SHORT') == 5
    assert get_max_k_for_regime(settings, 'PRESERVATION') == 0


def test_select_portfolio_kelly_multiplier():
    candidates = [
        Candidate('A', expected_pnl=10.0, success_probability=0.8, risk_score=30.0),
        Candidate('B', expected_pnl=9.0, success_probability=0.7, risk_score=40.0),
    ]
    corr = {('A', 'B'): 0.95, ('B', 'A'): 0.95}
    result = select_portfolio(candidates, corr, k=2, solver='brute')
    assert 0.65 <= result.kelly_multiplier <= 1.0


def test_select_portfolio_weights_sum_to_one():
    candidates = [
        Candidate('A', expected_pnl=10.0, success_probability=0.8, risk_score=30.0),
        Candidate('B', expected_pnl=5.0, success_probability=0.7, risk_score=40.0),
    ]
    result = select_portfolio(candidates, {}, k=2, solver='brute')
    total = sum(result.weights.values())
    assert total == pytest.approx(1.0, abs=1e-4)


def test_select_for_regime_with_open_symbols_respects_limit():
    settings = _settings_with_qubo(max_positions_spot_bull=3)
    candidates = [
        Candidate('A', expected_pnl=10.0, success_probability=0.8, risk_score=30.0),
        Candidate('B', expected_pnl=9.0, success_probability=0.7, risk_score=40.0),
        Candidate('C', expected_pnl=8.0, success_probability=0.6, risk_score=50.0),
    ]
    result = select_for_regime(candidates, {}, settings, 'SPOT_BULL', open_symbols=['A'])
    # A is locked, K=3 so at most 2 new selections
    assert len(result.selected) <= 3
    assert any(c.symbol == 'A' for c in result.selected)

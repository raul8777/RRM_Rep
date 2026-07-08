import pytest
from spot_correlation import _pearson, _kelly_reduction


def test_pearson_identical_series():
    a = list(range(1, 13))
    b = list(range(1, 13))
    assert _pearson(a, b) == pytest.approx(1.0, abs=1e-3)


def test_pearson_inverse_series():
    a = list(range(1, 13))
    b = list(reversed(range(1, 13)))
    assert _pearson(a, b) == pytest.approx(-1.0, abs=1e-3)


def test_pearson_zero_variance():
    a = [2.0, 2.0, 2.0, 2.0, 2.0]
    b = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _pearson(a, b) is None


def test_pearson_too_few_points():
    assert _pearson([1.0, 2.0], [3.0, 4.0]) is None


def test_kelly_reduction_no_correlation():
    assert _kelly_reduction(0.0) == 1.0
    assert _kelly_reduction(0.59) == 1.0


def test_kelly_reduction_perfect_correlation():
    assert _kelly_reduction(1.0) == pytest.approx(0.65, abs=1e-2)


def test_kelly_reduction_high_correlation():
    mult = _kelly_reduction(0.95)
    assert 0.65 < mult < 1.0


def test_kelly_reduction_mid_correlation():
    mult = _kelly_reduction(0.8)
    assert 0.65 < mult < 1.0

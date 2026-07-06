"""Tests for pair_screener.py regime-aware screening logic."""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from pair_screener import (
    get_screened_pairs,
    invalidate_cache,
    _score_for_regime,
)


TICKERS = [
    {"symbol": "BTCUSDT", "lastPrice": "65000", "quoteVolume": "500000000", "priceChangePercent": "2.5"},
    {"symbol": "ETHUSDT", "lastPrice": "3500", "quoteVolume": "300000000", "priceChangePercent": "1.2"},
    {"symbol": "SOLUSDT", "lastPrice": "150", "quoteVolume": "150000000", "priceChangePercent": "-3.0"},
    {"symbol": "DOGEUSDT", "lastPrice": "0.16", "quoteVolume": "120000000", "priceChangePercent": "-0.5"},
    {"symbol": "SHIBUSDT", "lastPrice": "0.00002", "quoteVolume": "50000000", "priceChangePercent": "5.0"},
    {"symbol": "XRPUSDT", "lastPrice": "0.55", "quoteVolume": "40000000", "priceChangePercent": "0.8"},
    {"symbol": "PEPEUSDT", "lastPrice": "0.000001", "quoteVolume": "25000000", "priceChangePercent": "12.0"},
    # Below spot min vol
    {"symbol": "LOWVOLUSDT", "lastPrice": "1.0", "quoteVolume": "1000000", "priceChangePercent": "20.0"},
]


class _MockResponse:
    def __init__(self, data):
        self._data = json.dumps(data).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _mock_urlopen(data):
    def _urlopen(url, *args, **kwargs):
        return _MockResponse(data)
    return _urlopen


@pytest.fixture(autouse=True)
def _reset_cache():
    invalidate_cache()
    yield
    invalidate_cache()


class TestScoreForRegime:
    def test_spot_bull_rewards_positive_momentum(self):
        c = {"vol_usdt": 100_000_000, "chg_pct": 5.0}
        scores = _score_for_regime(c, "SPOT_BULL")
        assert scores["spot_score"] > 0
        assert scores["range_score"] == scores["futures_score"]

    def test_futures_short_zeroes_spot_score(self):
        c = {"vol_usdt": 100_000_000, "chg_pct": 5.0}
        scores = _score_for_regime(c, "FUTURES_SHORT")
        assert scores["spot_score"] == 0.0
        assert scores["range_score"] == 0.0
        assert scores["futures_short_score"] > 0

    def test_range_scalp_uses_absolute_change(self):
        c_up = {"vol_usdt": 100_000_000, "chg_pct": 5.0}
        c_down = {"vol_usdt": 100_000_000, "chg_pct": -5.0}
        assert _score_for_regime(c_up, "RANGE_SCALP")["spot_score"] == _score_for_regime(c_down, "RANGE_SCALP")["spot_score"]


class TestGetScreenedPairs:
    def test_spot_bull_returns_bullish_symbols(self):
        with patch("pair_screener.urllib.request.urlopen", _mock_urlopen(TICKERS)):
            result = get_screened_pairs(spot_min_vol=10_000_000, regime_mode="SPOT_BULL")
        assert "BTCUSDT" in result["spot"]
        assert "SOLUSDT" not in result["spot"][:3]
        assert result["_meta"]["regime_mode"] == "SPOT_BULL"

    def test_futures_short_returns_empty_spot(self):
        with patch("pair_screener.urllib.request.urlopen", _mock_urlopen(TICKERS)):
            result = get_screened_pairs(spot_min_vol=10_000_000, regime_mode="FUTURES_SHORT")
        assert result["spot"] == []
        assert "BTCUSDT" in result["futures"]
        assert result["_meta"]["regime_mode"] == "FUTURES_SHORT"

    def test_preservation_allows_only_narrative_tokens(self):
        class _Settings:
            narrative_tokens = ["XRPUSDT"]
        with patch("pair_screener.urllib.request.urlopen", _mock_urlopen(TICKERS)):
            result = get_screened_pairs(
                spot_min_vol=10_000_000,
                regime_mode="PRESERVATION",
                settings=_Settings(),
            )
        assert result["spot"] == ["XRPUSDT"]

    def test_range_scalp_ranks_by_absolute_volatility(self):
        with patch("pair_screener.urllib.request.urlopen", _mock_urlopen(TICKERS)):
            result = get_screened_pairs(spot_min_vol=10_000_000, regime_mode="RANGE_SCALP")
        # BTC has the largest absolute score (vol * abs(chg)) among eligible symbols
        assert result["spot"][0] == "BTCUSDT"
        # A falling but volatile symbol is still ranked high in range mode
        assert "SOLUSDT" in result["spot"]

    def test_persistence_writes_to_db(self):
        db = MagicMock()
        with patch("pair_screener.urllib.request.urlopen", _mock_urlopen(TICKERS)):
            get_screened_pairs(
                spot_min_vol=10_000_000,
                regime_mode="SPOT_BULL",
                db_client=db,
            )
        assert db.execute_update.call_count >= 1, "execute_update should be called at least once"
        # First call is the screener_decisions INSERT
        first_call_args = db.execute_update.call_args_list[0][0][1]
        assert first_call_args[0] == "SPOT_BULL"
        assert json.loads(first_call_args[1])  # spot_symbols

    def test_cache_invalidated_on_regime_change(self):
        call_count = {"n": 0}
        original = _mock_urlopen(TICKERS)
        def _counting_urlopen(*args, **kwargs):
            call_count["n"] += 1
            return original(*args, **kwargs)
        with patch("pair_screener.urllib.request.urlopen", _counting_urlopen):
            r1 = get_screened_pairs(spot_min_vol=10_000_000, regime_mode="FUTURES_SHORT")
            assert r1["_meta"]["regime_mode"] == "FUTURES_SHORT"
            r2 = get_screened_pairs(spot_min_vol=10_000_000, regime_mode="SPOT_BULL")
            assert r2["_meta"]["regime_mode"] == "SPOT_BULL"
        assert call_count["n"] == 2

"""tests/test_lifecycle_signal.py — SignalPipeline tests."""

import pytest
from lifecycle.base import MarketContext, SignalPolicy, Side
from lifecycle.signal import SignalPipeline, analysis_to_signal


def _ctx(regime="SPOT_BULL", confidence=None) -> MarketContext:
    return MarketContext(
        timestamp=0.0,
        regime=regime,
        regime_confidence=confidence or {regime: 0.8, "RANGE_SCALP": 0.1, "PRESERVATION": 0.05, "FUTURES_SHORT": 0.05},
        btc_7d_pct=0.0,
        btc_30d_pct=0.0,
        btc_day_pct=0.0,
        fear_greed_index=50,
        btc_dominance=55.0,
    )


def _analysis(symbol, signal="BUY", strength=0.7, pnl=1.0, prob=0.55, liquidity=10_000_000):
    return {
        "symbol": symbol,
        "signal": signal,
        "strength": strength,
        "expected_pnl": pnl,
        "success_probability": prob,
        "liquidity_usdt_24h": liquidity,
    }


class TestSignalPipeline:
    def test_basic_filter(self):
        ctx = _ctx("SPOT_BULL")
        policy = SignalPolicy(
            min_signal_strength=0.5,
            min_success_probability=0.5,
            min_expected_pnl=0.0,
            min_liquidity_usdt=3_000_000,
            categories_enabled=("LARGE_CAP", "MIDCAP_ALT", "NARRATIVE", "MEME"),
            max_candidates=5,
            diversification_mode="category",
        )
        analyses = [
            _analysis("BTCUSDT", strength=0.8, pnl=2.0, prob=0.6, liquidity=50_000_000),
            _analysis("ETHUSDT", strength=0.3, pnl=1.0, prob=0.55, liquidity=40_000_000),  # too weak
            _analysis("FETUSDT", strength=0.7, pnl=1.5, prob=0.52, liquidity=5_000_000),
        ]
        pipe = SignalPipeline()
        signals = pipe.evaluate(analyses, ctx, policy)
        assert len(signals) == 2
        assert all(s.symbol in {"BTCUSDT", "FETUSDT"} for s in signals)
        assert signals[0].composite_score >= signals[1].composite_score

    def test_diversification_by_category(self):
        ctx = _ctx("SPOT_BULL")
        policy = SignalPolicy(
            min_signal_strength=0.0,
            min_success_probability=0.0,
            min_expected_pnl=-9999.0,
            min_liquidity_usdt=0.0,
            categories_enabled=("LARGE_CAP", "MIDCAP_ALT", "NARRATIVE"),
            max_candidates=3,
            diversification_mode="category",
        )
        # Give MIDCAP a much higher score than LARGE_CAP, but category mode should still pick one of each.
        analyses = [
            _analysis("SOLUSDT", strength=0.95, pnl=3.0, prob=0.7),  # MIDCAP
            _analysis("ADAUSDT", strength=0.92, pnl=2.5, prob=0.68),  # MIDCAP
            _analysis("BTCUSDT", strength=0.60, pnl=1.0, prob=0.55),  # LARGE_CAP
            _analysis("FETUSDT", strength=0.70, pnl=1.5, prob=0.60),  # NARRATIVE
        ]
        pipe = SignalPipeline()
        signals = pipe.evaluate(analyses, ctx, policy)
        symbols = {s.symbol for s in signals}
        # With 3 slots and category-first diversification, at least one per category.
        assert "BTCUSDT" in symbols
        assert "FETUSDT" in symbols
        assert any(s.symbol in {"SOLUSDT", "ADAUSDT"} for s in signals)

    def test_short_side_only_if_eligible(self):
        ctx = _ctx("FUTURES_SHORT", confidence={"FUTURES_SHORT": 0.85, "SPOT_BULL": 0.05, "RANGE_SCALP": 0.05, "PRESERVATION": 0.05})
        policy = SignalPolicy(
            min_signal_strength=0.0,
            min_success_probability=0.0,
            min_expected_pnl=-9999.0,
            min_liquidity_usdt=0.0,
            categories_enabled=("LARGE_CAP", "MIDCAP_ALT"),
            max_candidates=2,
            diversification_mode="none",
        )
        analyses = [
            _analysis("BTCUSDT", signal="SELL", strength=0.8, pnl=2.0, prob=0.6),
            _analysis("ETHUSDT", signal="SELL", strength=0.8, pnl=2.0, prob=0.6),  # not eligible
        ]
        pipe = SignalPipeline(short_eligible={"BTCUSDT"})
        signals = pipe.evaluate(analyses, ctx, policy)
        assert len(signals) == 1
        assert signals[0].symbol == "BTCUSDT"
        assert signals[0].side == Side.SHORT

    def test_regime_alignment_filter(self):
        ctx = _ctx("FUTURES_SHORT", confidence={"FUTURES_SHORT": 0.85, "SPOT_BULL": 0.05, "RANGE_SCALP": 0.05, "PRESERVATION": 0.05})
        policy = SignalPolicy(
            min_signal_strength=0.0,
            min_success_probability=0.0,
            min_expected_pnl=-9999.0,
            min_liquidity_usdt=0.0,
            categories_enabled=("LARGE_CAP",),
            max_candidates=1,
            diversification_mode="none",
        )
        analyses = [_analysis("BTCUSDT", signal="BUY", strength=0.9, pnl=2.0, prob=0.6)]
        pipe = SignalPipeline()
        signals = pipe.evaluate(analyses, ctx, policy)
        assert len(signals) == 0  # LONG in FUTURES_SHORT with high confidence is rejected

    def test_analysis_to_signal_one_shot(self):
        ctx = _ctx("SPOT_BULL")
        analysis = _analysis("BTCUSDT", strength=0.8, pnl=2.0, prob=0.6)
        signal = analysis_to_signal(analysis, ctx)
        assert signal is not None
        assert signal.symbol == "BTCUSDT"
        assert signal.side == Side.LONG
        assert signal.category == "LARGE_CAP"

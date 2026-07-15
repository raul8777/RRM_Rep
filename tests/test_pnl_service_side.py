from types import SimpleNamespace

import pnl_service


class _FakeClient:
    def get_symbol_price(self, symbol):
        return 100.0


class _FakePredictor:
    def __init__(self):
        self.calls = []

    def predict_pnl(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "expected_pnl": 0.5,
            "success_probability": 0.61,
            "bootstrap_mode": False,
        }


def test_infers_short_side_for_futures_short_regime(monkeypatch):
    fake = _FakePredictor()
    monkeypatch.setattr(pnl_service, "get_predictor", lambda: fake)

    settings = SimpleNamespace(current_regime_mode="FUTURES_SHORT", trade_horizon_minutes=120)
    precomputed_signal = {"indicators": {"rsi": {"value": 60}}}

    pred, _ = pnl_service.predict_for_symbol(
        symbol="BTCUSDT",
        client=_FakeClient(),
        settings=settings,
        position_size_usdt=100.0,
        precomputed_signal=precomputed_signal,
        trade_type="futures",
    )

    assert pred is not None
    assert fake.calls
    assert fake.calls[-1]["trade_side"] == "SHORT"


def test_explicit_trade_side_overrides_inference(monkeypatch):
    fake = _FakePredictor()
    monkeypatch.setattr(pnl_service, "get_predictor", lambda: fake)

    settings = SimpleNamespace(current_regime_mode="FUTURES_SHORT", trade_horizon_minutes=120)
    precomputed_signal = {"indicators": {"rsi": {"value": 60}}}

    pred, _ = pnl_service.predict_for_symbol(
        symbol="BTCUSDT",
        client=_FakeClient(),
        settings=settings,
        position_size_usdt=100.0,
        precomputed_signal=precomputed_signal,
        trade_type="futures",
        trade_side="LONG",
    )

    assert pred is not None
    assert fake.calls
    assert fake.calls[-1]["trade_side"] == "LONG"

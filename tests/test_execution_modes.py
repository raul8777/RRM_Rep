from config import Settings
from directional_arbiter import evaluate_range_scalp
from execution_modes import capital_bucket_for_mode, side_for_mode, trade_type_for_mode
from run_trades import _open_arbitrated_range_scalp_margin_short, _revalidate_entry_before_submit
import run_trades


def _prediction(expected_pnl=5.0, probability=0.65):
    return {
        "expected_pnl": expected_pnl,
        "success_probability": probability,
        "position_size_usdt": 100.0,
        "current_price": 100.0,
        "target_price": 96.0,
        "stop_price": 102.0,
        "trade_side": "SHORT",
        "risk_metrics": {"max_loss_usdt": 10.0},
    }


def test_spot_margin_short_mode_contract():
    assert side_for_mode("SPOT_MARGIN_SHORT") == "SHORT"
    assert trade_type_for_mode("SPOT_MARGIN_SHORT") == "spot_margin"
    assert capital_bucket_for_mode("SPOT_MARGIN_SHORT") == "spot"


def test_pre_submit_revalidation_fails_closed_without_fresh_price():
    class MissingPriceClient:
        def get_price(self, _symbol):
            raise RuntimeError("feed unavailable")

    result = _revalidate_entry_before_submit(
        "BTCUSDT",
        "LONG",
        MissingPriceClient(),
        2.0,
        {"resistance_level": 110.0},
    )

    assert result == (False, "PRICE_REFRESH_FAILED", None)


def test_pre_submit_revalidation_rejects_zero_risk():
    class PriceClient:
        def get_price(self, _symbol):
            return 100.0

    ok, reason, price = _revalidate_entry_before_submit(
        "BTCUSDT",
        "SHORT",
        PriceClient(),
        0.0,
        {"support_level": 95.0},
    )

    assert ok is False
    assert reason == "ZERO_RISK_DISTANCE"
    assert price == 100.0


def test_staging_routes_eligible_range_short_to_spot_margin():
    settings = Settings()
    result = evaluate_range_scalp(
        "BTCUSDT",
        {"sr_zone_position": 0.9, "resistance_bounce_sell": True},
        _prediction(-1.0, 0.4),
        _prediction(-1.0, 0.4),
        _prediction(),
        True,
        settings,
    )

    assert result.selected_mode == "SPOT_MARGIN_SHORT"
    assert result.capital_intent == "SPOT_MARGIN"


def test_margin_short_does_not_require_futures_contract():
    settings = Settings()
    result = evaluate_range_scalp(
        "SPOTONLYUSDT",
        {"sr_zone_position": 0.9, "resistance_bounce_sell": True},
        _prediction(-1.0, 0.4),
        None,
        _prediction(),
        False,
        settings,
        short_executable=True,
    )

    assert result.selected_mode == "SPOT_MARGIN_SHORT"
    assert result.short_executable is True
    assert result.futures_executable is False


def test_margin_opener_requires_and_persists_complete_thesis():
    class MarginClient:
        db_client = None

        def open_short(self, symbol, amount, **kwargs):
            self.call = (symbol, amount, kwargs)
            return type("Position", (), {
                "quantity": 1.0,
                "entry_price": 100.0,
                "stop_price": 102.0,
                "target_price": 96.0,
                "position_id": "position-1",
                "thesis_id": "thesis-1",
                "close_group_id": "group-1",
            })()

    client = MarginClient()
    settings = Settings(dry_run=False, binance_use_testnet=True)
    decision = {
        "selected_mode": "SPOT_MARGIN_SHORT",
        "locked_position_size_usdt": 100.0,
        "selected_thesis_type": "SHORT_RESISTANCE_REJECTION",
        "selected_contract": {
            "side": "SHORT", "thesis_type": "SHORT_RESISTANCE_REJECTION",
            "entry_price": 100.0, "margin_usdt": 100.0, "leverage": 1,
            "qty": 1.0, "notional_usdt": 100.0,
            "effective_target_price": 96.0, "effective_stop_price": 102.0,
            "economic_risk_usdt": 2.2, "economic_reward_usdt": 4.0,
            "projected_entry_fee_usdt": 0.05, "projected_exit_fee_usdt": 0.1,
            "slippage_reserve_usdt": 0.05, "funding_reserve_usdt": 0.0,
            "expected_net_pnl_usdt": 1.0, "expected_return_on_risk": 0.45,
            "executable": True,
        },
        "short_prediction": {
            "expected_pnl": 5.0,
            "success_probability": 0.65,
            "target_price": 96.0,
            "stop_price": 102.0,
        },
    }

    order = _open_arbitrated_range_scalp_margin_short(
        "BTCUSDT",
        decision,
        settings,
        {"indicators": {}},
        margin_client=client,
    )

    assert order["side"] == "SHORT_SPOT_MARGIN"
    assert order["position_id"] == "position-1"
    assert client.call[2]["metadata"]["thesis_snapshot_v1"]["side"] == "SHORT"


def test_margin_factory_blocks_opening_when_restart_recovery_fails(monkeypatch):
    class MarginClient:
        def __init__(self, *args, db_client=None, **kwargs):
            self.db_client = db_client

        def recover_open_positions(self):
            return [{"position_id": "position-1", "status": "error", "error": "debt mismatch"}]

    monkeypatch.setattr(run_trades, "SpotMarginClient", MarginClient)
    monkeypatch.setattr(run_trades, "_spot_margin_client", None)
    monkeypatch.setattr(run_trades, "_spot_margin_recovery_done", False)
    settings = Settings(dry_run=False, binance_use_testnet=True)

    try:
        run_trades._get_spot_margin_client(settings, db_client=object())
    except RuntimeError as exc:
        assert "recovery blocked" in str(exc)
    else:
        raise AssertionError("Margin factory must block after a failed recovery")
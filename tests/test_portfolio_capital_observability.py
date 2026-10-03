from types import SimpleNamespace

from fastapi.testclient import TestClient

import app as app_module


class _SpotClient:
    def get_account_balance_details(self):
        return {
            "USDT": {"free": 2611.0, "locked": 12.0, "total": 2623.0},
            "BNB": {"free": 0.02, "locked": 0.0, "total": 0.02},
        }

    def get_symbol_price(self, symbol):
        return {"BNBUSDT": 600.0}.get(symbol, 1.0)


class _FuturesClient:
    def get_account_info(self):
        return {
            "totalWalletBalance": "806.51",
            "totalMarginBalance": "806.51",
            "availableBalance": "806.51",
            "totalInitialMargin": "5800.00",
            "totalMaintMargin": "390.00",
            "totalOpenOrderInitialMargin": "200.00",
            "totalPositionInitialMargin": "5600.00",
            "maxWithdrawAmount": "806.51",
            "assets": [
                {
                    "asset": "USDT",
                    "walletBalance": "806.51",
                    "availableBalance": "806.51",
                    "crossWalletBalance": "806.51",
                    "positionInitialMargin": "5600.00",
                    "openOrderInitialMargin": "200.00",
                    "initialMargin": "5800.00",
                    "maintMargin": "390.00",
                },
                {"asset": "BNB", "walletBalance": "0.01", "availableBalance": "0"},
                {"asset": "USDC", "walletBalance": "5000.0", "availableBalance": "5000.0"},
                {"asset": "BTC", "walletBalance": "0.01", "availableBalance": "0.01"},
            ],
            "multiAssetsMargin": False,
            "positions": [
                {"symbol": "BTCUSDT", "positionAmt": "0", "positionSide": "BOTH"},
            ],
        }

    def get_price(self, symbol):
        return {"BNBUSDT": 600.0, "USDCUSDT": 1.0, "BTCUSDT": 80000.0}[symbol]


def _install_fakes(monkeypatch):
    spot_client = _SpotClient()
    futures_client = _FuturesClient()
    class _PortfolioManager:
        def proactive_liquidity_targets(self):
            return {
                "spot_entry_max_usdt": 1500.0,
                "spot_min_ready_usdt": 1500.0,
                "spot_target_usdt": 1510.0,
                "futures_entry_max_usdt": 2500.0,
                "futures_min_ready_usdt": 2500.0,
                "futures_target_usdt": 2510.0,
                "buffer_usdt": 10.0,
            }

    class _DB:
        def execute_query(self, _query, _params=None):
            return []

    components = SimpleNamespace(
        binance_client=spot_client,
        futures_client=futures_client,
        db_client=_DB(),
        portfolio_manager=_PortfolioManager(),
    )
    settings = SimpleNamespace(
        directional_policy_mode="active",
        range_scalp_ranking_mode="ev_net",
        portfolio_proactive_liquidity_enabled=True,
    )
    monkeypatch.setattr(app_module, "get_components", lambda: components)
    monkeypatch.setattr(app_module, "settings", settings)
    monkeypatch.setattr(
        app_module.run_trades,
        "_get_futures_client",
        lambda *_args, **_kwargs: futures_client,
    )
    monkeypatch.setattr(
        "binance_commission.get_bnb_reserve_usdt",
        lambda _settings: {
            "spot_reserve_usdt": 25.0,
            "futures_reserve_usdt": 30.0,
            "total_reserve_usdt": 55.0,
        },
    )
    monkeypatch.setattr(app_module, "_get_pause_new_entries_status", lambda: {"active": True})
    monkeypatch.delenv("DASHBOARD_API_KEY", raising=False)
    return spot_client, futures_client


def test_portfolio_capital_exposes_locked_and_encumbered_balances(monkeypatch):
    _install_fakes(monkeypatch)
    response = TestClient(app_module.app).get("/portfolio/capital")

    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is True
    assert result["spot"]["usdt"] == {"free": 2611.0, "locked": 12.0, "total": 2623.0}
    assert result["spot"]["wallet_value_usdt_equivalent"] == 2635.0
    assert result["futures"]["account"]["availableBalance"] == 806.51
    assert result["futures"]["multi_assets_margin"] is False
    assert result["futures"]["wallet_value_usdt_equivalent"] == 6612.51
    assert result["futures"]["assets"]["USDC"]["walletBalance"] == 5000.0
    assert result["futures"]["assets"]["BTC"]["walletBalance"] == 0.01
    assert result["futures"]["account"]["totalOpenOrderInitialMargin"] == 200.0
    assert result["futures"]["account"]["totalPositionInitialMargin"] == 5600.0
    assert result["bnb_reserves_usdt"]["spot_reserve_usdt"] == 25.0
    assert result["proactive_liquidity"]["targets"]["spot_target_usdt"] == 1510.0
    assert result["proactive_liquidity"]["targets"]["futures_target_usdt"] == 2510.0
    assert result["proactive_liquidity"]["spot"]["spendable_usdt"] == 2586.0
    assert result["proactive_liquidity"]["futures"]["spendable_usdt"] == 776.51
    assert result["proactive_liquidity"]["spot"]["shortfall_to_entry_usdt"] == 0.0
    assert result["entry_policy"]["capital_bridge_transfers_enabled"] is False
    assert result["entry_policy"]["bnb_auto_funding_enabled_before_entry"] is False


def test_portfolio_capital_keeps_failed_source_unknown_not_zero(monkeypatch):
    spot_client, _futures_client = _install_fakes(monkeypatch)

    def _raise_balance_error():
        raise RuntimeError("spot account unavailable")

    monkeypatch.setattr(spot_client, "get_account_balance_details", _raise_balance_error)
    response = TestClient(app_module.app).get("/portfolio/capital")

    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is False
    assert result["spot"]["status"] == "error"
    assert result["spot"]["usdt"] is None
    assert result["errors"]["spot"] == "spot account unavailable"
    assert result["proactive_liquidity"]["spot"]["spendable_usdt"] is None
    assert result["proactive_liquidity"]["spot"]["shortfall_to_entry_usdt"] is None
from binance_client import BinanceClient
import hashlib
import json
from types import SimpleNamespace


def test_binance_client_initialization():
    client = BinanceClient(api_key="test", api_secret="test", testnet=True)
    assert client.testnet is True
    assert client.client.API_URL == "https://demo-api.binance.com/api"


def test_symbol_filters_include_complete_exchange_snapshot():
    client = BinanceClient.__new__(BinanceClient)
    client.testnet = True
    client._filters_cache = {}
    client.client = SimpleNamespace(get_symbol_info=lambda _symbol: {
        "filters": [
            {"filterType": "LOT_SIZE", "minQty": "0.01", "maxQty": "100", "stepSize": "0.01"},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.01", "maxQty": "100", "stepSize": "0.01"},
            {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
        ],
    })

    filters = client.get_symbol_filters("TESTUSDT")

    assert filters["exchange_filters_validated"] is True
    assert filters["min_notional"] == 5.0
    assert len(filters["filter_snapshot"]) == 4
    canonical = json.dumps(filters["filter_snapshot"], sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert filters["filter_snapshot_sha256"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert filters["filter_snapshot_venue"] == "spot_demo"
    assert filters["filter_snapshot_symbol"] == "TESTUSDT"
    assert filters["filter_snapshot_retrieved_at"]


def test_symbol_filter_cache_retains_original_snapshot_capture_time():
    client = BinanceClient.__new__(BinanceClient)
    client.testnet = True
    client._filters_cache = {}
    info = {"filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.1"}]}
    client.client = SimpleNamespace(get_symbol_info=lambda _symbol: info)

    first = client.get_symbol_filters("TESTUSDT")
    info["filters"] = [{"filterType": "PRICE_FILTER", "tickSize": "0.5"}]
    second = client.get_symbol_filters("TESTUSDT")

    assert second["filter_snapshot_retrieved_at"] == first["filter_snapshot_retrieved_at"]
    assert second["filter_snapshot_sha256"] == first["filter_snapshot_sha256"]


def test_symbol_filters_mark_fallback_values_unverified():
    client = BinanceClient.__new__(BinanceClient)
    client.testnet = True
    client._filters_cache = {}
    client.client = SimpleNamespace(get_symbol_info=lambda _symbol: {"filters": []})

    filters = client.get_symbol_filters("TESTUSDT")

    assert filters["exchange_filters_validated"] is False
    assert filters["min_notional"] == 10.0

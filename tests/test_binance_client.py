from binance_client import BinanceClient


def test_binance_client_initialization():
    client = BinanceClient(api_key="test", api_secret="test", testnet=True)
    assert client.testnet is True
    assert "testnet" in client.client.API_URL or client.client.API_URL.endswith("vision/api")

import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from Dev.scripts import capture_universal_entry_sandbox_commissions as capture


def test_signed_get_is_get_only_sandbox_allowlisted_and_hmac_signed(monkeypatch):
    from email.message import Message

    class FakeResponse:
        status = 200
        headers = Message()
        headers["Date"] = "Thu, 01 Oct 2026 20:00:00 GMT"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"symbol":"BTCUSDT","makerCommissionRate":"0.0002"}'

    seen = []

    def fake_open(request, timeout):
        seen.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(capture.urllib.request, "urlopen", fake_open)
    payload, headers, elapsed = capture._signed_get(
        "https://testnet.binancefuture.com", "/fapi/v1/commissionRate",
        {"symbol": "BTCUSDT", "recvWindow": 5000, "timestamp": 123456},
        api_key="test-key", api_secret="test-secret",
    )
    request, timeout = seen[0]
    query, signature = request.full_url.split("?", 1)[1].rsplit("&signature=", 1)
    assert request.get_method() == "GET"
    assert request.host == "testnet.binancefuture.com"
    assert request.headers["X-mbx-apikey"] == "test-key"
    assert signature == hmac.new(b"test-secret", query.encode(), hashlib.sha256).hexdigest()
    assert timeout == 20
    assert payload["symbol"] == "BTCUSDT"
    assert headers["Date"] == "Thu, 01 Oct 2026 20:00:00 GMT"
    assert elapsed >= 0
    with pytest.raises(ValueError, match="non-sandbox"):
        capture._signed_get("https://fapi.binance.com", "/fapi/v1/commissionRate", {}, api_key="k", api_secret="s")


def test_current_commission_snapshot_never_claims_historical_fee_or_scoring(tmp_path, monkeypatch):
    normalized = tmp_path / "normalized"
    for venue in ("spot", "um"):
        folder = normalized / venue
        folder.mkdir(parents=True)
        (folder / "BTCUSDT_1m.manifest.json").write_text(json.dumps({"symbol": "BTCUSDT", "contract_symbol": "BTCUSDT"}), encoding="utf-8")
    monkeypatch.setattr(capture, "NORMALIZED_ROOT", normalized)
    monkeypatch.setattr(capture, "_server_clock", lambda *_args: (1_800_000_000_000, "Thu, 01 Oct 2026 20:00:00 GMT"))
    monkeypatch.setattr(capture, "_signed_get", lambda host, path, params, **kwargs: (
        {"symbol": params["symbol"], "makerCommissionRate": "0.0002", "takerCommissionRate": "0.0004"}
        if "fapi" in path else {"symbol": params["symbol"], "standardCommission": {"maker": "0.001", "taker": "0.001"}},
        {"Date": "Thu, 01 Oct 2026 20:00:00 GMT"}, 0.01,
    ))

    report = capture.capture_commissions("key", "secret", output_root=tmp_path / "out")

    assert report["read_only"] is True
    assert report["orders_submitted"] is False
    assert report["historical_fees_verified"] is False
    assert report["formula_scoring_allowed"] is False
    assert report["venues"]["spot_demo"]["verified_symbols"] == 1
    assert report["venues"]["usd_m_futures_testnet"]["verified_symbols"] == 1
    saved = json.loads((tmp_path / "out" / Path(report["path"]).name).read_text(encoding="utf-8"))
    assert "secret" not in json.dumps(saved)
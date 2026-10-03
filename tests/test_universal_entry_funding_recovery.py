import hashlib
import json

import pandas as pd
import pytest

from Dev.scripts.recover_universal_entry_funding import START_MS, END_MS, recover_funding


def _fixture(tmp_path):
    source = tmp_path / "funding.parquet"
    pd.DataFrame([{
        "symbol": "BTCUSDT", "funding_time": pd.Timestamp("2025-02-05T00:00:00Z"),
        "funding_rate": 0.0001,
        "source_url": "https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT",
    }]).to_parquet(source)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"files": [{"file": source.name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]}), encoding="utf-8")
    normalized = tmp_path / "normalized"
    normalized.mkdir()
    for symbol in ("BTCUSDT", "1000PEPEUSDT"):
        (normalized / f"{symbol}_1m.manifest.json").write_text(json.dumps({"contract_symbol": symbol}), encoding="utf-8")
    return source, manifest, normalized, tmp_path / "recovery.json"


def _response(monkeypatch, rows):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(rows).encode("utf-8")

    def fake_open(request, timeout):
        assert request.full_url.startswith("https://fapi.binance.com/fapi/v1/fundingRate?symbol=1000PEPEUSDT")
        assert timeout == 30
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_open)


def test_funding_recovery_preserves_public_source_and_no_testnet_claim(tmp_path, monkeypatch):
    source, manifest, normalized, output = _fixture(tmp_path)
    _response(monkeypatch, [{
        "symbol": "1000PEPEUSDT", "fundingTime": START_MS + 1,
        "fundingRate": "0.0001", "markPrice": "0.0101",
    }])

    report = recover_funding(source, manifest, normalized, output)

    assert report["local_pilot_contracts"] == 1
    assert report["recovered_rows"] == 1
    assert report["source_venue"] == "usd_m_public_production_only"
    assert report["testnet_historical_funding_verified"] is False
    assert report["formula_scoring_allowed"] is False
    assert hashlib.sha256(output.with_name("public_production_funding_1000PEPEUSDT_20250205_20250220.json").read_bytes()).hexdigest() == report["recovered_raw_sha256"]


@pytest.mark.parametrize("rows", [
    [{"symbol": "1000PEPEUSDT", "fundingTime": END_MS, "fundingRate": "0.0001", "markPrice": "0.01"}],
    [{"symbol": "BTCUSDT", "fundingTime": START_MS, "fundingRate": "0.0001", "markPrice": "0.01"}],
    [{"symbol": "1000PEPEUSDT", "fundingTime": START_MS, "fundingRate": "0.0001", "markPrice": "0.01"}] * 2,
])
def test_funding_recovery_rejects_invalid_response_without_writing(tmp_path, monkeypatch, rows):
    source, manifest, normalized, output = _fixture(tmp_path)
    _response(monkeypatch, rows)

    with pytest.raises(ValueError):
        recover_funding(source, manifest, normalized, output)
    assert not output.exists()
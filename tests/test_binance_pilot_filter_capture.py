import pytest
from email.message import Message
from urllib.parse import parse_qs, urlparse

from Dev.scripts.capture_binance_pilot_filter_snapshots import _audit_server_time, _request_exchange_info, _request_server_time, _selected_response_headers, _summarize_response_samples, _symbol_record


def test_symbol_record_hashes_raw_filters_and_marks_current_capture():
    filters = [
        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100"},
        {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
    ]
    payload = {"serverTime": 123456, "symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "filters": filters}]}

    record = _symbol_record(payload, "BTCUSDT", venue="spot_demo", raw_sha256="a" * 64, captured_at="2026-09-30T00:00:00+00:00")

    assert record["status"] == "TRADING"
    assert record["exchange_filters_validated"] is True
    assert record["filter_snapshot_sha256"]
    assert record["filter_snapshot_retrieved_at"] == "2026-09-30T00:00:00+00:00"
    assert record["filter_snapshot_venue"] == "spot_demo"


def test_exchange_info_rejects_non_allowlisted_or_non_https_host():
    with pytest.raises(ValueError, match="not allowlisted"):
        _request_exchange_info("https://api.binance.com/api/v3/exchangeInfo")
    with pytest.raises(ValueError, match="not allowlisted"):
        _request_exchange_info("http://demo-api.binance.com/api/v3/exchangeInfo")
    with pytest.raises(ValueError, match="not allowlisted"):
        _request_server_time("https://api.binance.com/api/v3/time")


def test_exchange_info_bypasses_caches_and_records_cache_status(monkeypatch):
    requests = []

    class FakeResponse:
        status = 200
        headers = Message()

        def __init__(self):
            self.headers["X-Cache"] = "Miss from cloudfront"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"serverTime":123,"symbols":[]}'

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    for _ in range(2):
        _, _, response_headers = _request_exchange_info("https://testnet.binancefuture.com/fapi/v1/exchangeInfo")
        assert response_headers["X-Cache"] == "Miss from cloudfront"
    _request_exchange_info("https://demo-api.binance.com/api/v3/exchangeInfo")

    assert len({request.full_url for request, _ in requests[:2]}) == 2
    assert urlparse(requests[2][0].full_url).query == ""
    for request, timeout in requests:
        request_headers = {name.lower(): value for name, value in request.header_items()}
        assert request_headers["cache-control"] == "no-cache, no-store, max-age=0"
        assert request_headers["pragma"] == "no-cache"
        assert timeout == 20
    assert parse_qs(urlparse(requests[0][0].full_url).query)["_pilot_capture_nonce"]
    assert parse_qs(urlparse(requests[1][0].full_url).query)["_pilot_capture_nonce"]


def test_exchange_info_server_time_must_match_public_time_endpoint():
    assert _audit_server_time(1_000_000, 1_030_000)["status"] == "fresh_within_tolerance"
    stale = _audit_server_time(1_000_000, 1_061_000)
    assert stale["status"] == "inconsistent"
    assert stale["skew_ms"] == -61_000
    assert _audit_server_time(None, 1_000_000)["status"] == "unverifiable_missing_exchange_info_server_time"


def test_capture_retains_only_nonsecret_freshness_headers():
    headers = {
        "Date": "Thu, 01 Oct 2026 05:28:43 GMT",
        "Cache-Control": "no-cache, no-store",
        "Via": "CloudFront",
        "X-Cache": "Miss from cloudfront",
        "Set-Cookie": "not-captured",
    }

    assert _selected_response_headers(headers) == {
        "Date": "Thu, 01 Oct 2026 05:28:43 GMT",
        "Cache-Control": "no-cache, no-store",
        "Via": "CloudFront",
        "X-Cache": "Miss from cloudfront",
    }


def test_repeated_exchange_info_audit_flags_backend_snapshot_drift():
    samples = [
        {"exchange_info_sha256": "a", "exchange_info_catalog_sha256_excluding_server_time": "catalog-a", "server_time": 100, "pilot_filter_fingerprints": {"BTCUSDT": "filters-a"}},
        {"exchange_info_sha256": "b", "exchange_info_catalog_sha256_excluding_server_time": "catalog-b", "server_time": 200, "pilot_filter_fingerprints": {"BTCUSDT": "filters-b"}},
        {"exchange_info_sha256": "b", "exchange_info_catalog_sha256_excluding_server_time": "catalog-b", "server_time": 200, "pilot_filter_fingerprints": {"BTCUSDT": "filters-b"}},
    ]

    result = _summarize_response_samples(samples)

    assert result["sample_count"] == 3
    assert result["unique_exchange_info_body_hashes"] == 2
    assert result["unique_server_times"] == 2
    assert result["unstable_pilot_filter_fingerprints"] == ["BTCUSDT"]
    assert result["pilot_filters_stable"] is False
    assert result["body_variation_limited_to_server_time"] is False
    assert result["responses_consistent"] is False


def test_repeated_exchange_info_can_have_stable_pilot_filters_with_changing_global_body():
    samples = [
        {"exchange_info_sha256": "a", "exchange_info_catalog_sha256_excluding_server_time": "catalog-a", "server_time": 100, "pilot_filter_fingerprints": {"BTCUSDT": "filter-a", "ETHUSDT": "filter-b"}},
        {"exchange_info_sha256": "b", "exchange_info_catalog_sha256_excluding_server_time": "catalog-a", "server_time": 200, "pilot_filter_fingerprints": {"BTCUSDT": "filter-a", "ETHUSDT": "filter-b"}},
        {"exchange_info_sha256": "b", "exchange_info_catalog_sha256_excluding_server_time": "catalog-a", "server_time": 200, "pilot_filter_fingerprints": {"BTCUSDT": "filter-a", "ETHUSDT": "filter-b"}},
    ]

    result = _summarize_response_samples(samples)

    assert result["unique_exchange_info_body_hashes"] == 2
    assert result["unique_server_times"] == 2
    assert result["pilot_filter_fingerprint_count"] == 2
    assert result["pilot_filters_stable"] is True
    assert result["body_variation_limited_to_server_time"] is True
    assert result["responses_consistent"] is False


def test_missing_symbol_is_not_filled_from_current_or_other_venue():
    record = _symbol_record({"symbols": []}, "DELISTEDUSDT", venue="usd_m_futures_testnet", raw_sha256="b" * 64, captured_at="2026-09-30T00:00:00+00:00")

    assert record["status"] == "not_listed_at_capture"
    assert record["exchange_filters_validated"] is False
    assert record["filter_snapshot"] is None

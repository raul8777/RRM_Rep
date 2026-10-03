import csv
import io
import json
import zipfile
from datetime import date

import pytest

from Dev.scripts import acquire_universal_entry_features as features


def test_pilot_feature_audit_counts_verified_and_missing_days_without_network(tmp_path, monkeypatch):
    normalized = tmp_path / "normalized"
    normalized.mkdir()
    (normalized / "PEPEUSDT_1m.manifest.json").write_text(json.dumps({"contract_symbol": "1000PEPEUSDT"}), encoding="utf-8")
    seen = []

    def fake_existing(job, path):
        seen.append((job, path))
        return {**job, "status": "already_verified"} if job["date"] == "2025-02-05" else None

    monkeypatch.setattr(features, "_existing_result", fake_existing)
    report = features.audit_pilot_development_features("metrics", normalized_root=normalized, raw_root=tmp_path)

    assert report["expected_archive_count"] == 15
    assert report["result_counts"] == {"already_verified": 1, "missing_archive": 14}
    assert report["all_full_days_verified"] is False
    assert report["formula_scoring_allowed"] is False
    assert report["testnet_account_or_filter_history_verified"] is False
    assert all(row["contract_symbol"] == "1000PEPEUSDT" for row, _ in seen)
    assert "um/metrics/1000PEPEUSDT" in seen[0][1].as_posix()


def test_metrics_archive_rejects_wrong_contract_symbol(tmp_path):
    path = tmp_path / "BTCUSDT-metrics-2025-02-05.zip"
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
                     "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
                     "count_long_short_ratio", "sum_taker_long_short_vol_ratio"])
    writer.writerow(["2025-02-05 00:00:00", "ETHUSDT", "1", "1", "1", "1", "1", "1"])
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("BTCUSDT-metrics-2025-02-05.csv", stream.getvalue())

    with pytest.raises(ValueError, match="metrics symbol mismatch"):
        features._inspect_archive(path, "metrics", date(2025, 2, 5), contract_symbol="BTCUSDT")
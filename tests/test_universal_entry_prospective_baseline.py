import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from Dev.scripts import freeze_universal_entry_prospective_baseline as baseline


def _settings(**overrides):
    values = {
        "app_env": "staging", "binance_use_testnet": True,
        "dry_run": False, "allow_real_trading": True,
        "futures_enabled": True, "futures_leverage": 2,
        "max_loss_usd_per_trade": 18.0, "max_per_trade_usdt": 100.0,
        "staging_pilot_max_order_notional_usdt": 100.0,
        "futures_usdt_per_trade": 100.0, "max_positions": 1,
        "max_positions_per_symbol": 1, "futures_max_new_per_cycle": 1,
        "max_positions_spot_bull": 1, "max_positions_range_scalp": 1,
        "max_positions_preservation": 1, "max_positions_futures_short": 1,
        "futures_max_shorts": 1, "futures_max_longs": 1, "futures_max_largecap_shorts": 1,
        "overflow_max_extra": 0, "futures_overflow_max_extra": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_baseline_freeze_is_non_secret_scoped_and_hashes_sources(tmp_path, monkeypatch):
    pilot = tmp_path / "Dev/benchmarks/universal_entry_selection"
    output = pilot / "pilot_development"
    output.mkdir(parents=True)
    for venue in ("spot", "um"):
        folder = pilot / "normalized/pilot_development" / venue
        folder.mkdir(parents=True)
        (folder / "BTCUSDT_1m.manifest.json").write_text(json.dumps({"symbol": "BTCUSDT", "contract_symbol": "BTCUSDT"}), encoding="utf-8")
    for name in ("philosophy_proposals.jsonl", "bilateral_contract_scaffold.jsonl", "bilateral_scaffold_outcomes.jsonl", "bilateral_raw_predictions.jsonl"):
        (output / name).write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(baseline, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(baseline, "PILOT_ROOT", pilot)
    monkeypatch.setattr(baseline, "OUTPUT_ROOT", output)
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("BINANCE_USE_TESTNET", "true")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ALLOW_REAL_TRADING", "true")

    report = baseline.build_baseline_manifest(settings=_settings(), output_root=output)
    saved = json.loads((tmp_path / report["path"]).read_text(encoding="utf-8"))
    serialized = json.dumps(saved)

    assert saved["scope"] == "prospective_staging_functional_feasibility_only"
    assert saved["selector"] == "baseline_current_universal"
    assert saved["historical_1154_161_cohort_reproduced"] is False
    assert saved["formula_comparison_in_scope"] is False
    assert saved["orders_submitted_by_baseline_capture"] is False
    assert saved["safety"]["effective_limits_verified"] is False
    assert saved["safety"]["cross_venue_single_position_guard_verified"] is False
    assert saved["matched_development_symbols"] == ["BTCUSDT"]
    assert "api_secret" not in serialized.lower()
    assert "api_key" not in serialized.lower()

    oversized = baseline.build_baseline_manifest(
        settings=_settings(max_per_trade_usdt=1500.0, staging_pilot_max_order_notional_usdt=150.0,
                           futures_usdt_per_trade=150.0, max_positions=4),
        output_root=output,
    )
    oversized_saved = json.loads((tmp_path / oversized["path"]).read_text(encoding="utf-8"))
    assert oversized_saved["safety"]["effective_limits_verified"] is False


def test_baseline_freeze_refuses_production_or_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(baseline, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(baseline, "PILOT_ROOT", tmp_path / "pilot")
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("BINANCE_USE_TESTNET", "true")
    with pytest.raises(ValueError, match="active sandbox execution mode"):
        baseline.build_baseline_manifest(settings=_settings(dry_run=True), output_root=tmp_path / "out")
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(ValueError, match="restricted to Binance Testnet Staging"):
        baseline.build_baseline_manifest(settings=_settings(), output_root=tmp_path / "out")
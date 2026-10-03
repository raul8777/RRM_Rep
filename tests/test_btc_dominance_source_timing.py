import pandas as pd

from Dev.scripts.build_peak_calibration_dataset import align_btc_dominance, load_btc_dominance


def test_backdated_coingecko_snapshot_is_not_historical_data(tmp_path):
    external = tmp_path / "historical_data/external"
    external.mkdir(parents=True)
    pd.DataFrame([{
        "date": "2025-02-05",
        "btc_dominance": 56.0,
        "snapshot_time": "2026-07-11T03:00:00Z",
    }]).to_parquet(external / "btc_dominance_20250205_20260711.parquet")

    assert load_btc_dominance(tmp_path).empty


def test_btc_dominance_only_becomes_available_after_capture_and_expires():
    snapshot = pd.Timestamp("2025-02-05T12:30:00Z").tz_localize(None)
    dom = pd.DataFrame({"btc_dominance": [56.0]}, index=pd.DatetimeIndex([snapshot]))
    hours = pd.DatetimeIndex([
        pd.Timestamp("2025-02-05T12:00:00Z").tz_localize(None),
        pd.Timestamp("2025-02-05T13:00:00Z").tz_localize(None),
        pd.Timestamp("2025-02-06T13:00:00Z").tz_localize(None),
    ])

    aligned = align_btc_dominance(dom, hours)

    assert pd.isna(aligned.iloc[0])
    assert aligned.iloc[1] == 56.0
    assert pd.isna(aligned.iloc[2])
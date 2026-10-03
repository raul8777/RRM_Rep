import json
from pathlib import Path

from Dev.scripts.verify_universal_entry_replay_parity import verify_parity


def _write_fixture(path: Path) -> None:
    import pandas as pd

    start = pd.Timestamp("2025-02-05T00:00:00Z")
    rows = []
    for index in range(20):
        price = 100.0 + index
        rows.append({
            "open_time": (start + pd.Timedelta(minutes=index)).timestamp(),
            "open": price,
            "high": price + 0.1,
            "low": price - 0.1,
            "close": price + 0.05,
            "volume": 1.0,
        })
    pd.DataFrame(rows).to_parquet(path / "BTCUSDT_1m.parquet", index=False)


def test_replay_parity_report_passes_on_real_feed_shape(tmp_path: Path):
    _write_fixture(tmp_path)

    report = verify_parity(tmp_path, ["BTCUSDT"], sample_per_symbol=5)

    assert report["cases"] == 10
    assert report["mismatches"] == 0
    assert report["parity_passed"] is True
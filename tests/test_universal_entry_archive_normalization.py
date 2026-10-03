import csv
import hashlib
import io
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import pandas as pd

from Dev.scripts.normalize_universal_entry_archives import normalize_archive_range
from simulation.mimic_feed import HistoricalDataFeed


def _write_daily_archive(
    root: Path,
    venue: str,
    symbol: str,
    day: date,
    *,
    header: bool,
    legacy: bool = False,
    contract_symbol: str | None = None,
) -> None:
    raw_root = root / "raw"
    archive_symbol = contract_symbol or symbol
    target_dir = raw_root if legacy else raw_root / venue / archive_symbol / "1m"
    target_dir.mkdir(parents=True)
    member = io.StringIO(newline="")
    writer = csv.writer(member)
    if header:
        writer.writerow(("open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades"))
    start = int(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp())
    divisor = 1000 if venue == "um" else 1_000_000
    for index in range(1440):
        timestamp = (start + index * 60) * divisor
        writer.writerow((timestamp, "100", "101", "99", "100.5", "2", timestamp + 59 * divisor, "201", "3"))
    filename = f"{archive_symbol}-1m-{day.isoformat()}-{venue}.zip" if legacy else f"{archive_symbol}-1m-{day.isoformat()}.zip"
    archive_path = target_dir / filename
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{archive_symbol}-1m-{day.isoformat()}.csv", member.getvalue())
    archive_path.with_suffix(archive_path.suffix + ".CHECKSUM").write_text(
        hashlib.sha256(archive_path.read_bytes()).hexdigest(), encoding="ascii"
    )


@pytest.mark.parametrize("venue,header", [("spot", False), ("um", True)])
def test_normalizes_verified_daily_archive_for_mimic_feed(tmp_path: Path, venue: str, header: bool) -> None:
    day = date(2025, 2, 5)
    _write_daily_archive(tmp_path, venue, "BTCUSDT", day, header=header)

    report = normalize_archive_range(
        venue=venue,
        symbol="BTCUSDT",
        interval="1m",
        start_date=day,
        end_date=day,
        raw_root=tmp_path / "raw",
        output_root=tmp_path / "normalized",
    )

    assert report["rows"] == 1440
    assert Path(report["output"]).parent.name == venue
    bars = HistoricalDataFeed(str(tmp_path / "normalized" / venue)).get_klines("BTCUSDT", "1m")
    assert len(bars) == 1440
    assert bars[0]["open_time"] == datetime(2025, 2, 5, tzinfo=timezone.utc).timestamp()
    assert bars[0]["close"] == 100.5


def test_normalizer_refuses_to_replace_existing_output_without_flag(tmp_path: Path) -> None:
    day = date(2025, 2, 5)
    _write_daily_archive(tmp_path, "spot", "BTCUSDT", day, header=False)
    kwargs = {
        "venue": "spot",
        "symbol": "BTCUSDT",
        "interval": "1m",
        "start_date": day,
        "end_date": day,
        "raw_root": tmp_path / "raw",
        "output_root": tmp_path / "normalized",
    }
    normalize_archive_range(**kwargs)

    with pytest.raises(FileExistsError):
        normalize_archive_range(**kwargs)


def test_normalizer_rejects_checksum_mismatch(tmp_path: Path) -> None:
    day = date(2025, 2, 5)
    _write_daily_archive(tmp_path, "spot", "BTCUSDT", day, header=False)
    archive_path = tmp_path / "raw/spot/BTCUSDT/1m/BTCUSDT-1m-2025-02-05.zip"
    archive_path.with_suffix(archive_path.suffix + ".CHECKSUM").write_text("0" * 64, encoding="ascii")

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        normalize_archive_range(
            venue="spot",
            symbol="BTCUSDT",
            interval="1m",
            start_date=day,
            end_date=day,
            raw_root=tmp_path / "raw",
            output_root=tmp_path / "normalized",
        )


def test_normalizer_reads_verified_legacy_flat_archive(tmp_path: Path) -> None:
    day = date(2025, 2, 5)
    _write_daily_archive(tmp_path, "um", "BTCUSDT", day, header=True, legacy=True)

    report = normalize_archive_range(
        venue="um",
        symbol="BTCUSDT",
        interval="1m",
        start_date=day,
        end_date=day,
        raw_root=tmp_path / "raw",
        output_root=tmp_path / "normalized",
    )

    manifest = Path(report["manifest"]).read_text(encoding="utf-8")
    assert "BTCUSDT-1m-2025-02-05-um.zip" in manifest


def test_normalizer_resolves_usdm_1000x_symbol_alias(tmp_path: Path) -> None:
    day = date(2025, 2, 5)
    _write_daily_archive(
        tmp_path, "um", "PEPEUSDT", day, header=True, contract_symbol="1000PEPEUSDT",
    )

    report = normalize_archive_range(
        venue="um",
        symbol="PEPEUSDT",
        interval="1m",
        start_date=day,
        end_date=day,
        raw_root=tmp_path / "raw",
        output_root=tmp_path / "normalized",
    )

    assert report["symbol"] == "PEPEUSDT"
    manifest = Path(report["manifest"]).read_text(encoding="utf-8")
    assert '"contract_symbol": "1000PEPEUSDT"' in manifest


def test_mimic_feed_resamples_only_complete_utc_bars_from_one_minute(tmp_path: Path) -> None:
    start = pd.Timestamp("2025-02-05T00:00:00Z")
    timestamps = pd.date_range(start, periods=61, freq="min")
    frame = pd.DataFrame({
        "open_time": timestamps,
        "open": range(100, 161),
        "high": range(101, 162),
        "low": range(99, 160),
        "close": range(100, 161),
        "volume": [1.0] * 61,
    })
    frame.to_parquet(tmp_path / "BTCUSDT_1m.parquet", index=False)

    bars = HistoricalDataFeed(str(tmp_path)).get_klines("BTCUSDT", "1h")

    assert len(bars) == 1
    assert bars[0]["open_time"] == start.timestamp()
    assert bars[0]["open"] == 100
    assert bars[0]["high"] == 160
    assert bars[0]["low"] == 99
    assert bars[0]["close"] == 159
    assert bars[0]["volume"] == 60.0
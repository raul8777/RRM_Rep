from __future__ import annotations

import glob
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd


_REQUIRED_OHLCV = ("open_time", "open", "high", "low", "close", "volume")
_STAGE_INTERVALS = {
    "entry": ("1h",),
    "kelly": ("1h",),
    "lifecycle": ("1h",),
    "aic": ("1h",),
    "e2e": ("1h",),
}


def _file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamps(values: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(values):
        return pd.to_datetime(values, utc=True)
    numeric = pd.to_numeric(values, errors="coerce")
    finite = numeric.dropna()
    if finite.empty:
        return pd.to_datetime(values, utc=True, errors="coerce")
    magnitude = float(finite.abs().median())
    unit = "ms" if magnitude >= 100_000_000_000 else "s"
    return pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")


def _audit_parquet(path: str, interval: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {"path": path, "interval": interval, "ok": False}
    if not os.path.exists(path):
        result["errors"] = ["missing_file"]
        return result
    try:
        frame = pd.read_parquet(path, columns=list(_REQUIRED_OHLCV))
    except Exception as exc:
        result["errors"] = [f"read_error:{exc}"]
        return result

    errors: List[str] = []
    warnings: List[str] = []
    if frame.empty:
        errors.append("empty")
    missing = [column for column in _REQUIRED_OHLCV if column not in frame.columns]
    if missing:
        errors.append(f"missing_columns:{','.join(missing)}")
        result.update({"errors": errors, "warnings": warnings})
        return result

    timestamps = _timestamps(frame["open_time"])
    invalid_timestamps = int(timestamps.isna().sum())
    duplicate_timestamps = int(timestamps.duplicated().sum())
    if invalid_timestamps:
        errors.append(f"invalid_timestamps:{invalid_timestamps}")
    if duplicate_timestamps:
        errors.append(f"duplicate_timestamps:{duplicate_timestamps}")

    numeric = frame[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="coerce")
    nan_count = int(numeric.isna().sum().sum())
    invalid_price = int((numeric[["open", "high", "low", "close"]] <= 0).any(axis=1).sum())
    invalid_volume = int((numeric["volume"] < 0).sum())
    invalid_ohlc = int(
        (
            (numeric["low"] > numeric[["open", "close", "high"]].min(axis=1))
            | (numeric["high"] < numeric[["open", "close", "low"]].max(axis=1))
        ).sum()
    )
    if nan_count:
        errors.append(f"nan_values:{nan_count}")
    if invalid_price:
        errors.append(f"invalid_prices:{invalid_price}")
    if invalid_volume:
        errors.append(f"invalid_volume:{invalid_volume}")
    if invalid_ohlc:
        errors.append(f"invalid_ohlc:{invalid_ohlc}")

    expected = pd.Timedelta("1D" if interval == "1d" else interval)
    ordered = timestamps.dropna().sort_values()
    gaps = int((ordered.diff().dropna() > expected * 1.10).sum()) if len(ordered) > 1 else 0
    if gaps:
        warnings.append(f"gaps:{gaps}")

    result.update({
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "rows": int(len(frame)),
        "start": ordered.iloc[0].isoformat() if not ordered.empty else None,
        "end": ordered.iloc[-1].isoformat() if not ordered.empty else None,
        "gaps": gaps,
        "hash": _file_hash(path),
    })
    return result


def _external_coverage(data_dir: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    if not manifest.get("start") or not manifest.get("end"):
        return {
            "ticker_24h_historical_coverage_pct": None,
            "funding_range_covered": None,
            "fear_greed_coverage_pct": None,
            "btc_dominance_historical": False,
        }
    external_dir = os.path.join(data_dir, "external")
    start = pd.Timestamp(manifest["start"])
    end = pd.Timestamp(manifest["end"])
    expected_days = pd.date_range(start.normalize(), end.normalize(), freq="1D")
    ticker_days = {
        pd.Timestamp(os.path.basename(path).split("_")[-1].split(".")[0]).strftime("%Y%m%d")
        for path in glob.glob(os.path.join(external_dir, "ticker_24h_historical_*.parquet"))
    }
    expected_keys = {day.strftime("%Y%m%d") for day in expected_days}
    ticker_coverage = len(expected_keys & ticker_days) / max(1, len(expected_keys))

    funding_frames = []
    for path in glob.glob(os.path.join(external_dir, "funding_rate_*.parquet")):
        try:
            frame = pd.read_parquet(path)
            column = "funding_time" if "funding_time" in frame.columns else "fundingTime" if "fundingTime" in frame.columns else None
            if column:
                frame["_timestamp"] = _timestamps(frame[column])
                funding_frames.append(frame[["_timestamp"]])
        except Exception:
            continue
    funding_ok = False
    if funding_frames:
        funding = pd.concat(funding_frames, ignore_index=True)["_timestamp"].dropna()
        funding_ok = not funding.empty and funding.min() <= start and funding.max() >= end

    fng_dates = set()
    for path in glob.glob(os.path.join(external_dir, "fear_greed_*.parquet")):
        try:
            frame = pd.read_parquet(path)
            column = "timestamp" if "timestamp" in frame.columns else "date" if "date" in frame.columns else None
            if column:
                fng_dates.update(pd.to_datetime(frame[column], utc=True, errors="coerce").dropna().dt.strftime("%Y%m%d"))
        except Exception:
            continue
    fng_coverage = len(expected_keys & fng_dates) / max(1, len(expected_keys))
    return {
        "ticker_24h_historical_coverage_pct": round(ticker_coverage * 100.0, 2),
        "funding_range_covered": funding_ok,
        "fear_greed_coverage_pct": round(fng_coverage * 100.0, 2),
        "btc_dominance_historical": False,
    }


def certify_window(
    manifest: Dict[str, Any],
    data_dir: str,
    stage: str,
) -> Dict[str, Any]:
    stage = str(stage).lower()
    if stage not in _STAGE_INTERVALS:
        raise ValueError(f"Unsupported certification stage: {stage}")
    symbols = manifest.get("symbols") or manifest.get("baseline_symbols") or []
    checks: List[Dict[str, Any]] = []
    errors: List[str] = []
    warnings: List[str] = []

    for symbol in symbols:
        for interval in _STAGE_INTERVALS[stage]:
            check = _audit_parquet(os.path.join(data_dir, f"{symbol}_{interval}.parquet"), interval)
            check["symbol"] = symbol
            checks.append(check)
            if check["ok"] and manifest.get("start") and manifest.get("end"):
                manifest_start = pd.Timestamp(manifest["start"])
                manifest_end = pd.Timestamp(manifest["end"])
                data_start = pd.Timestamp(check["start"])
                data_end = pd.Timestamp(check["end"])
                if data_start > manifest_start or data_end < manifest_end:
                    check["ok"] = False
                    check["errors"].append("window_not_covered")
            if not check["ok"]:
                errors.append(f"{symbol}_{interval}")

    for interval in ("1h", "4h", "1d"):
        key = f"BTCUSDT_{interval}"
        if any(c.get("symbol") == "BTCUSDT" and c.get("interval") == interval for c in checks):
            continue
        check = _audit_parquet(os.path.join(data_dir, f"{key}.parquet"), interval)
        check["symbol"] = "BTCUSDT"
        checks.append(check)
        if check["ok"] and manifest.get("start") and manifest.get("end"):
            manifest_start = pd.Timestamp(manifest["start"])
            manifest_end = pd.Timestamp(manifest["end"])
            if pd.Timestamp(check["start"]) > manifest_start or pd.Timestamp(check["end"]) < manifest_end:
                try:
                    from simulation.mimic_feed import HistoricalDataFeed
                    derived = HistoricalDataFeed(data_dir).get_klines("BTCUSDT", interval)
                    derived_start = pd.Timestamp(min(row["open_time"] for row in derived), unit="s", tz="UTC")
                    derived_end = pd.Timestamp(max(row["open_time"] for row in derived), unit="s", tz="UTC")
                    if derived_start <= manifest_start and derived_end >= manifest_end:
                        check["ok"] = True
                        check["warnings"].append("derived_from_1h")
                        check["derived_start"] = derived_start.isoformat()
                        check["derived_end"] = derived_end.isoformat()
                    else:
                        check["ok"] = False
                        check["errors"].append("window_not_covered")
                except Exception:
                    check["ok"] = False
                    check["errors"].append("window_not_covered")
        if not check["ok"]:
            errors.append(key)

    external_dir = os.path.join(data_dir, "external")
    external = {}
    for prefix in ("funding_rate", "ticker_24h", "btc_dominance", "fear_greed"):
        matches = []
        if os.path.isdir(external_dir):
            matches = sorted(
                name for name in os.listdir(external_dir)
                if name.startswith(prefix) and name.endswith(".parquet")
            )
        external[prefix] = matches
        if not matches:
            warnings.append(f"missing_external:{prefix}")

    external_coverage = _external_coverage(data_dir, manifest)
    target_regime = str(manifest.get("forced_regime") or manifest.get("dominant_regime") or "").upper()
    if external_coverage["ticker_24h_historical_coverage_pct"] is not None and external_coverage["ticker_24h_historical_coverage_pct"] < 99.0:
        errors.append("ticker_24h_historical_coverage")
    if external_coverage["fear_greed_coverage_pct"] is not None and external_coverage["fear_greed_coverage_pct"] < 99.0:
        errors.append("fear_greed_coverage")
    if target_regime in {"PRESERVATION", "FUTURES_SHORT"} and not external_coverage["funding_range_covered"]:
        errors.append("funding_range_coverage")

    fine_intervals = {}
    for interval in ("1m", "5m", "15m"):
        covered = 0
        for symbol in symbols:
            path = os.path.join(data_dir, f"{symbol}_{interval}.parquet")
            if not os.path.exists(path):
                continue
            check = _audit_parquet(path, interval)
            if not check.get("ok"):
                continue
            if manifest.get("start") and manifest.get("end"):
                if pd.Timestamp(check["start"]) > pd.Timestamp(manifest["start"]):
                    continue
                if pd.Timestamp(check["end"]) < pd.Timestamp(manifest["end"]):
                    continue
            covered += 1
        fine_intervals[interval] = covered
    resolution = "high" if fine_intervals["1m"] == len(symbols) else "medium" if fine_intervals["5m"] == len(symbols) else "low"
    if stage in {"lifecycle", "aic", "e2e"} and resolution == "low":
        warnings.append("intrabar_fidelity_low")

    certificate = {
        "window_id": manifest.get("window_id"),
        "stage": stage,
        "status": "certified" if not errors else "rejected",
        "certified": not errors,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_hash": hashlib.sha256(json.dumps(manifest, sort_keys=True, default=str).encode()).hexdigest(),
        "symbols": len(symbols),
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "external": external,
        "external_coverage": external_coverage,
        "fine_interval_coverage": fine_intervals,
        "intrabar_fidelity": resolution,
    }
    return certificate


def certify_manifests(
    manifests: Iterable[Dict[str, Any]],
    data_dir: str,
    stage: str,
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    certificates = [certify_window(manifest, data_dir, stage) for manifest in manifests]
    report = {
        "stage": stage,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(certificates),
        "certified": sum(1 for certificate in certificates if certificate["certified"]),
        "rejected": sum(1 for certificate in certificates if not certificate["certified"]),
        "windows": certificates,
    }
    if output_path:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
    return report

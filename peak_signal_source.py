"""peak_signal_source.py — lector eficiente de peaks_all.parquet y generador de schedules A/B/C.

Lectura por lotes con pyarrow, predicción vectorizada con los modelos
peak_quality usando n_jobs=12, y agrupación por (window_id, peak_start_ts).
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from peak_quality import (
    PeakQualityModel,
    _REGIME_MODELS,
    _assign_profile,
    get_peak_quality_model,
)

logger = logging.getLogger(__name__)

DEFAULT_PARQUET = (
    Path(__file__).resolve().parent
    / "Dev"
    / "benchmarks"
    / "peaks_dataset"
    / "peaks_all.parquet"
)
DEFAULT_MODEL_DIR = (
    Path(__file__).resolve().parent
    / "Dev"
    / "benchmarks"
    / "peaks_dataset"
    / "peak_quality_model"
)

# Columnas mínimas que necesitamos para construir un análisis inyectable en lifecycle.
_REQUIRED_COLUMNS = [
    "window_id",
    "peak_start_ts",
    "peak_end_ts",
    "symbol",
    "direction",
    "peak_return",
    "s_mode_confirmed",
    # features numéricas requeridas por el predictor
    "s_atr",
    "s_rsi_4h",
    "s_range_position",
    "s_vol_spike",
    "s_ticker_quote_volume",
    "s_ticker_24h_volume",
    "s_fear_greed_value",
    "e_open",
]


def _resolve_regime(row: pd.Series) -> str:
    """Elige el régimen para buscar el modelo peak_quality."""
    for col in ("s_mode_confirmed", "e_mode_confirmed", "window_regime"):
        v = row.get(col)
        if pd.notna(v) and v:
            return str(v).strip().upper()
    return "SPOT_BULL"


def _expected_direction_for_model_key(model_key: str) -> str:
    """Devuelve LONG/SHORT según el mapeo de _REGIME_MODELS."""
    for regime, info in _REGIME_MODELS.items():
        if info.get("model") == model_key:
            return info.get("direction", "LONG")
    return "LONG"


def _predict_batch(
    df: pd.DataFrame,
    model: PeakQualityModel,
    n_jobs: int = 12,
) -> pd.DataFrame:
    """Predice peak_return para un batch y asigna perfil usando vectorización."""
    df = df.copy()
    # Mapear cada régimen a su model_key
    regime_map = {}
    for regime in df["regime"].unique():
        regime = str(regime).strip().upper()
        regime_map[regime] = model.resolve_model_key(regime, "LONG")
    df["model_key"] = df["regime"].map(regime_map)

    df["predicted_peak_return"] = np.nan
    for model_key, grp in df.groupby("model_key", sort=False):
        feature_cols = model.features_for(model_key)
        if not feature_cols:
            feature_cols = [c for c in df.columns if c.startswith("s_") and pd.api.types.is_numeric_dtype(df[c])]
        medians = model.medians_for(model_key)
        X = np.empty((len(grp), len(feature_cols)), dtype=float)
        for i, col in enumerate(feature_cols):
            series = grp.get(col)
            if series is None or not pd.api.types.is_numeric_dtype(series):
                fill = float(medians.get(col, 0.0))
                X[:, i] = fill
            else:
                arr = np.array(series.to_numpy(dtype=float, copy=False, na_value=np.nan), dtype=float, copy=True)
                m = float(medians.get(col, 0.0))
                np.nan_to_num(arr, copy=False, nan=m, posinf=m, neginf=m)
                X[:, i] = arr
        regressor = model._load_model(model_key)
        if hasattr(regressor, "n_jobs"):
            regressor.n_jobs = n_jobs
        preds = regressor.predict(X)
        df.loc[grp.index, "predicted_peak_return"] = preds
    return df


def _assign_profiles(df: pd.DataFrame, model: PeakQualityModel) -> pd.DataFrame:
    """Asigna A/B/C/descartado usando los umbrales de cada model_key."""
    profiles = pd.Series(index=df.index, dtype=object)
    exp_gains = pd.Series(index=df.index, dtype=float)
    for model_key, grp in df.groupby("model_key", sort=False):
        thresholds = model.thresholds_for(model_key)
        direction = _expected_direction_for_model_key(model_key)
        preds = grp["predicted_peak_return"].to_numpy(dtype=float, copy=False)
        expected = preds if direction == "LONG" else -preds
        exp_gains.loc[grp.index] = expected
        a_thr = float(thresholds.get("A", 0.0))
        b_thr = float(thresholds.get("B", 0.0))
        c_thr = float(thresholds.get("C", 0.0))
        prof = np.where(
            expected >= a_thr,
            "A",
            np.where(
                expected >= b_thr,
                "B",
                np.where(expected >= c_thr, "C", "discarded"),
            ),
        )
        profiles.loc[grp.index] = prof
    df["expected_gain"] = exp_gains
    df["peak_quality_profile"] = profiles
    return df


def _row_to_analysis(row: pd.Series) -> Dict[str, Any]:
    """Convierte una fila peak filtrada en un dict de análisis para SignalPipeline."""
    profile = row["peak_quality_profile"]
    expected_gain = float(row["expected_gain"])
    # expected_pnl en puntos porcentuales para que ev_score tenga escala razonable
    expected_pnl = expected_gain * 100.0
    strength = min(max(expected_pnl, 0.55), 0.95)
    success_probability = 0.75

    peak_return = float(row.get("peak_return") or 0.0)
    direction = str(row["direction"]).upper()
    entry_price = float(row.get("e_open") or row.get("s_open") or 0.0)
    if entry_price <= 0:
        entry_price = 1.0
    exit_price = entry_price * (1.0 + peak_return) if direction == "LONG" else entry_price * (1.0 - peak_return)

    # Incluimos suficientes features numéricas para que SignalPipeline y
    # profile_classifier tengan valores realistas.
    analysis: Dict[str, Any] = {
        "symbol": row["symbol"],
        "signal": "BUY" if direction == "LONG" else "SELL",
        "direction": direction,
        "strength": strength,
        "expected_pnl": expected_pnl,
        "success_probability": success_probability,
        "liquidity_usdt_24h": float(
            row.get("s_ticker_quote_volume") or row.get("s_ticker_24h_volume") or 1_000_000.0
        ),
        "atr": float(row.get("s_atr") or 0.0),
        "rsi_4h": float(row.get("s_rsi_4h") or 50.0),
        "range_position": float(row.get("s_range_position") or 0.5),
        "vol_spike": float(row.get("s_vol_spike") or 1.0),
        "confluence": 1.0,
        "range_score": 1.0,
        "vol_score": 1.0,
        "rsi_score": 1.0,
        "pnl_score": 0.0,
        "short_term_score": 0.6,
        "symbol_quality_score": 0.0,
        "peak_quality": expected_gain,  # fallback numérico
        "peak_quality_profile": profile,
        "peak_return": peak_return,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "source": "peak_mimic",
        "regime": row.get("s_mode_confirmed") or "SPOT_BULL",
    }
    return analysis


def build_peak_signal_schedule(
    parquet_path: Optional[str] = None,
    model_dir: Optional[str] = None,
    batch_size: int = 100_000,
    n_jobs: int = 12,
    only_window_ids: Optional[List[str]] = None,
    include_profiles: Optional[Tuple[str, ...]] = ("A", "B", "C"),
    max_batches: Optional[int] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Lee peaks_all.parquet y devuelve {window_id: [analysis, ...]} ordenado por peak_start_dt.

    - Filtra direction == expected_direction del modelo (solo señales LONG para
      SPOT_BULL/RANGE_SCALP/PRESERVATION; SHORT para FUTURES_SHORT).
    - Mantiene solo perfiles en include_profiles.
    - Usa n_jobs=12 en RandomForestRegressor.predict.
    """
    path = Path(parquet_path) if parquet_path else DEFAULT_PARQUET
    if not path.exists():
        raise FileNotFoundError(f"peak parquet not found: {path}")

    model = get_peak_quality_model(model_dir or str(DEFAULT_MODEL_DIR))
    if not model.available():
        raise RuntimeError(f"peak_quality metadata not found in {model_dir or DEFAULT_MODEL_DIR}")

    pf = pq.ParquetFile(path)
    schema_cols = set(pf.schema_arrow.names)
    missing = [c for c in _REQUIRED_COLUMNS if c not in schema_cols]
    if missing:
        raise ValueError(f"Columnas faltantes en {path}: {missing}")

    # Columnas a leer: las requeridas + todas las s_* numéricas para predicción
    s_cols = [c for c in schema_cols if c.startswith("s_")]
    read_cols = list({*_REQUIRED_COLUMNS, *s_cols, "peak_return"})

    schedule: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    total_rows = 0
    kept_rows = 0

    for batch_num, batch in enumerate(pf.iter_batches(batch_size=batch_size, columns=read_cols)):
        if max_batches is not None and batch_num >= max_batches:
            break
        df = batch.to_pandas()

        # Timestamps en UTC (aware) para comparar con runner.start
        df["peak_start_dt"] = pd.to_datetime(df["peak_start_ts"], unit="us", utc=True)
        df["peak_end_dt"] = pd.to_datetime(df["peak_end_ts"], unit="us", utc=True)

        # Normalizar strings y limpiar NAs
        for col in ("window_id", "direction", "s_mode_confirmed", "e_mode_confirmed", "window_regime"):
            if col in df.columns:
                df[col] = df[col].astype(str).replace("nan", pd.NA).replace("<NA>", pd.NA)

        df["direction"] = df["direction"].str.upper()
        df = df.dropna(subset=["window_id", "direction"])
        df["regime"] = df.apply(_resolve_regime, axis=1)

        # Predicción vectorizada
        df = _predict_batch(df, model, n_jobs=n_jobs)
        df = _assign_profiles(df, model)

        # Filtrar dirección coherente con el modelo
        df["model_expected_direction"] = df["model_key"].apply(_expected_direction_for_model_key)
        df = df[df["direction"] == df["model_expected_direction"]]

        # Filtrar perfiles deseados
        df = df[df["peak_quality_profile"].isin(list(include_profiles))]

        # Filtrar por window_id si se pidió
        if only_window_ids is not None:
            df = df[df["window_id"].astype(str).isin(only_window_ids)]

        total_rows += len(batch)
        kept_rows += len(df)

        for _, row in df.iterrows():
            analysis = _row_to_analysis(row)
            analysis["peak_start_dt"] = row["peak_start_dt"].to_pydatetime()
            analysis["peak_end_dt"] = row["peak_end_dt"].to_pydatetime()
            window_id = str(row["window_id"])
            schedule[window_id].append(analysis)

    # Ordenar cada lista por timestamp
    for window_id in schedule:
        schedule[window_id].sort(key=lambda x: x["peak_start_dt"])

    logger.info(
        "peak_signal_schedule: %d filas leídas, %d eventos A/B/C retenidos en %d ventanas",
        total_rows,
        kept_rows,
        len(schedule),
    )
    return dict(schedule)


def load_schedule_for_window(
    parquet_path: str,
    window_id: str,
    model_dir: Optional[str] = None,
    batch_size: int = 100_000,
    n_jobs: int = 12,
    include_profiles: Tuple[str, ...] = ("A", "B", "C"),
) -> List[Dict[str, Any]]:
    """Shortcut para cargar el schedule de una sola ventana."""
    schedule = build_peak_signal_schedule(
        parquet_path=parquet_path,
        model_dir=model_dir,
        batch_size=batch_size,
        n_jobs=n_jobs,
        only_window_ids=[window_id],
        include_profiles=include_profiles,
    )
    return schedule.get(window_id, [])


if __name__ == "__main__":
    # Ejemplo rápido de validación: generar schedule y contar perfiles.
    logging.basicConfig(level=logging.INFO)
    out = build_peak_signal_schedule(batch_size=200_000, n_jobs=12)
    print("ventanas:", list(out.keys())[:5])
    if out:
        first = next(iter(out.values()))
        print("primer evento:", first[0] if first else None)

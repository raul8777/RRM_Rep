"""Super MIMIC Meta-Learner.

Learns which configs work in which market conditions by:
  1. Computing a regime fingerprint (BTC trend, RSI, dominance, F&G, vol spike, win rate).
  2. Storing and querying experiment results from super_mimic_experiments.
  3. Recommending warm-start configs for new experiments based on fingerprint similarity.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# Features that compose the fingerprint. All are normalized to ~[0,1] or z-scored.
FINGERPRINT_FEATURES = [
    "btc_trend_1d",
    "btc_trend_4h",
    "btc_trend_1h",
    "btc_rsi_4h",
    "btc_dominance",
    "fng_index",
    "avg_vol_spike",
    "win_rate_7d",
]


@dataclass
class RegimeFingerprint:
    """Continuous market fingerprint used for similarity search."""
    mode: str = ""
    values: Dict[str, float] = field(default_factory=dict)
    captured_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "values": self.values,
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RegimeFingerprint":
        return cls(
            mode=d.get("mode", ""),
            values=d.get("values", {}),
            captured_at=d.get("captured_at", datetime.now(timezone.utc).isoformat()),
        )

    def vector(self) -> np.ndarray:
        """Return ordered numpy vector for distance calculations."""
        return np.array([self.values.get(k, 0.0) for k in FINGERPRINT_FEATURES], dtype=float)

    def distance(self, other: "RegimeFingerprint") -> float:
        """Euclidean distance in fingerprint space (features are internally normalized)."""
        a = self.vector()
        b = other.vector()
        if np.linalg.norm(a) == 0 and np.linalg.norm(b) == 0:
            return 0.0
        return float(np.linalg.norm(a - b))


class SuperMimicMetaLearner:
    """Meta-learner for Super MIMIC configs across regimes."""

    def __init__(self, db_client=None):
        self.db_client = db_client
        self._cache: Dict[str, Any] = {}

    # ── Public API ──────────────────────────────────────────────────────────

    def capture_fingerprint(
        self,
        mode: str = "",
        binance_client=None,
        historical_dir: Optional[str] = None,
    ) -> RegimeFingerprint:
        """Build current regime fingerprint from live data or fallback files."""
        values = {}
        try:
            values = self._from_binance(binance_client)
        except Exception:
            values = self._from_files(historical_dir)

        # Normalize / clip to keep distances sane
        values["btc_rsi_4h"] = min(max(values.get("btc_rsi_4h", 50.0), 0.0), 100.0) / 100.0
        values["fng_index"] = min(max(values.get("fng_index", 50.0), 0.0), 100.0) / 100.0
        values["btc_dominance"] = min(max(values.get("btc_dominance", 0.5), 0.0), 1.0)
        values["avg_vol_spike"] = min(max(values.get("avg_vol_spike", 1.0), 0.0), 5.0) / 5.0
        values["win_rate_7d"] = min(max(values.get("win_rate_7d", 0.5), 0.0), 1.0)

        # Trends are already in [-1, 1] roughly; clip to keep range
        for key in ["btc_trend_1d", "btc_trend_4h", "btc_trend_1h"]:
            values[key] = max(-1.0, min(1.0, values.get(key, 0.0)))

        return RegimeFingerprint(mode=mode, values=values)

    def recommend_warm_start(
        self,
        fingerprint: RegimeFingerprint,
        mode: str,
        profile: str,
        top_k: int = 5,
        min_trades: int = 3,
    ) -> List[Dict[str, Any]]:
        """Recommend warm-start configs from historically similar regimes."""
        rows = self._load_completed_experiments(mode, profile)
        scored = []
        for row in rows:
            rec = (row.get("recommended_config") or {})
            if not rec:
                continue
            metrics = self._extract_metrics(row)
            if (metrics.get("trades", 0) or 0) < min_trades:
                continue
            try:
                raw_fp = row.get("fingerprint") or {"values": {}, "mode": ""}
                if isinstance(raw_fp, str):
                    raw_fp = json.loads(raw_fp)
                fp = RegimeFingerprint.from_dict(raw_fp)
            except Exception:
                continue
            dist = fingerprint.distance(fp)
            # Score: lower distance + higher PnL/day
            pnl_day = metrics.get("pnl_per_day_usdt", 0.0)
            score = pnl_day - dist * 10.0  # distance penalty
            scored.append({
                "experiment_id": row.get("id"),
                "config": rec,
                "metrics": metrics,
                "fingerprint": fp.to_dict(),
                "distance": dist,
                "score": score,
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    def record_experiment_result(
        self,
        experiment_id: str,
        fingerprint: RegimeFingerprint,
        metrics: Dict[str, Any],
    ) -> None:
        """Persist fingerprint and extracted metrics for an experiment."""
        db = self._get_db()
        db.execute_update(
            """
            UPDATE super_mimic_experiments
            SET fingerprint = %s, metrics = %s
            WHERE id = %s
            """,
            (
                json.dumps(fingerprint.to_dict()),
                json.dumps(metrics),
                experiment_id,
            ),
        )

    def learn_from_recent_experiments(
        self,
        lookback: int = 90,
    ) -> Dict[str, Any]:
        """Summarize what configs worked best per (mode, profile) recently."""
        db = self._get_db()
        rows = db.execute_query(
            """
            SELECT mode, profile, recommended_config, metrics, fingerprint, created_at
            FROM super_mimic_experiments
            WHERE status = 'completed'
              AND recommended_config IS NOT NULL
              AND created_at >= NOW() - INTERVAL '%s days'
            ORDER BY created_at DESC
            """,
            (lookback,),
        )
        summary = {}
        for row in rows:
            key = f"{row['mode']}/{row['profile']}"
            metrics = self._extract_metrics(row)
            if metrics.get("trades", 0) < 3:
                continue
            entry = {
                "config": row["recommended_config"],
                "pnl_per_day": metrics.get("pnl_per_day_usdt", 0.0),
                "win_rate": metrics.get("win_rate", 0.0),
                "sharpe": metrics.get("sharpe", 0.0),
                "fingerprint": row.get("fingerprint"),
            }
            summary.setdefault(key, []).append(entry)
        return summary

    # ── Learning engine retrain bridge ─────────────────────────────────────

    def retrain_predictor(self, min_trades: int = 5, lookback_days: int = 30) -> Dict[str, Any]:
        """Trigger learning_engine.run() to update indicator weights."""
        try:
            from learning_engine import LearningEngine
            le = LearningEngine(db_client=self._get_db())
            return le.run(min_trades=min_trades, lookback_days=lookback_days)
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    # ── Internals ───────────────────────────────────────────────────────────

    def _get_db(self):
        if self.db_client is not None:
            return self.db_client
        from init_components import get_components
        return get_components().db_client

    def _load_completed_experiments(self, mode: str, profile: str) -> List[Dict[str, Any]]:
        db = self._get_db()
        return db.execute_query(
            """
            SELECT id, recommended_config, metrics, fingerprint, created_at
            FROM super_mimic_experiments
            WHERE status = 'completed'
              AND mode = %s
              AND profile = %s
            ORDER BY created_at DESC
            LIMIT 200
            """,
            (mode, profile),
        ) or []

    def _extract_metrics(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Extract comparable metrics from DB row (works with JSON string or dict)."""
        raw = row.get("metrics") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}

        # Try multiple possible key layouts
        def get(*keys):
            for k in keys:
                if k in raw:
                    return raw[k]
                # nested in best recommendation
                for rec in raw.get("recommendations", []):
                    if k in rec:
                        return rec[k]
            return 0.0

        return {
            "pnl_per_day_usdt": get("pnl_per_day_usdt", "best_pnl_per_day_usdt", "pnl_usdt"),
            "win_rate": get("win_rate", "best_win_rate", "win_rate_pct"),
            "sharpe": get("sharpe", "best_sharpe"),
            "trades": get("trades", "best_trades", "total_trades"),
            "drawdown_pct": get("drawdown_pct", "best_drawdown_pct", "max_drawdown_pct"),
        }

    def _from_binance(self, binance_client) -> Dict[str, float]:
        """Fetch live-ish data from python-binance client."""
        if binance_client is None:
            raise ValueError("No binance_client provided")
        client = getattr(binance_client, "client", binance_client)

        def trend(interval: str, short: int, long: int) -> float:
            k = client.get_klines(symbol="BTCUSDT", interval=interval, limit=max(long + 5, 50))
            closes = [float(x[4]) for x in k]
            if len(closes) < long:
                return 0.0
            ema_short = self._ema(closes[-short:], short)
            ema_long = self._ema(closes[-long:], long)
            return (ema_short - ema_long) / ema_long if ema_long else 0.0

        def rsi(interval: str, period: int = 14) -> float:
            k = client.get_klines(symbol="BTCUSDT", interval=interval, limit=period + 10)
            closes = [float(x[4]) for x in k]
            return self._rsi(closes, period)

        values = {
            "btc_trend_1d": trend("1d", 9, 21),
            "btc_trend_4h": trend("4h", 9, 21),
            "btc_trend_1h": trend("1h", 9, 21),
            "btc_rsi_4h": rsi("4h", 14),
        }

        # BTC dominance / fear-greed: best effort from external files
        values.update(self._from_files())
        return values

    def _from_files(self, historical_dir: Optional[str] = None) -> Dict[str, float]:
        """Fallback fingerprint data from parquet files."""
        if historical_dir is None:
            historical_dir = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "historical_data",
                "external",
            )
        values = {}
        try:
            import pandas as pd
            fg_path = os.path.join(historical_dir, "fear_greed_20260101_20260711.parquet")
            if os.path.exists(fg_path):
                df = pd.read_parquet(fg_path)
                values["fng_index"] = float(df.iloc[-1]["value"]) if "value" in df.columns else 50.0
            dom_path = os.path.join(historical_dir, "btc_dominance_20260101_20260711.parquet")
            if os.path.exists(dom_path):
                df = pd.read_parquet(dom_path)
                values["btc_dominance"] = float(df.iloc[-1]["dominance"]) if "dominance" in df.columns else 0.5
        except Exception:
            values.setdefault("fng_index", 50.0)
            values.setdefault("btc_dominance", 0.5)
        values.setdefault("avg_vol_spike", 1.0)
        values.setdefault("win_rate_7d", 0.5)
        return values

    @staticmethod
    def _ema(values: List[float], period: int) -> float:
        if not values or len(values) < period:
            return 0.0
        arr = np.array(values[-period:])
        weights = np.exp(np.linspace(-1.0, 0.0, period))
        weights /= weights.sum()
        return float(np.convolve(arr, weights, mode="valid")[-1])

    @staticmethod
    def _rsi(closes: List[float], period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = float(np.mean(gains[-period:]))
        avg_loss = float(np.mean(losses[-period:]))
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

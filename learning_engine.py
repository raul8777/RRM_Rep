"""
Learning Engine Module

Analyses closed positions from the DB, detects which indicators correlated with
profitable trades, calculates per-symbol prediction bias, and produces adjusted
indicator weights that PnLPredictor can load to improve future predictions.

Self-learning loop:
  trades_closed in DB  →  LearningEngine.run()  →  indicator_weights table
  PnLPredictor.load_learned_weights(symbol)  →  better predictions  →  more trades
"""

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Hard limits to prevent a single indicator dominating
_WEIGHT_MIN = 0.02
_WEIGHT_MAX = 0.35
_BIAS_CORRECTION_MIN = 0.5
_BIAS_CORRECTION_MAX = 2.0

# Weights expire after 7 days — forces re-learn when market regime changes
_WEIGHT_TTL_DAYS = 7


class WeightAdjustment:
    """Proposed change to indicator weights for one symbol."""

    def __init__(self, symbol: str, weights: Dict[str, float],
                 bias_correction: float, metadata: Dict):
        self.symbol = symbol
        self.weights = weights
        self.bias_correction = bias_correction
        self.metadata = metadata

    def __repr__(self):
        return (f"WeightAdjustment(symbol={self.symbol}, "
                f"weights={self.weights}, bias={self.bias_correction:.3f})")


class LearningEngine:
    """Learns indicator weights from closed trade outcomes."""

    # In-memory cache so we don't hammer the DB on every cycle
    _cache: Dict[str, Tuple[Dict, float]] = {}
    _CACHE_TTL = 300.0  # 5 min

    def __init__(self, db_client=None):
        self.db = db_client
        self._last_run: Optional[datetime] = None
        self._last_result: Optional[Dict] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self, min_trades: int = 5, lookback_days: int = 30) -> Dict[str, Any]:
        """
        Full learning cycle: read DB → analyse → persist weights.

        Returns:
            Summary dict with accuracy, adjustments, and recommendations.
        """
        from position_manager import PositionManager
        if self.db is None:
            try:
                from init_components import get_components
                self.db = get_components().db_client
            except Exception:
                pass

        closed = self._load_closed_positions(lookback_days)
        if len(closed) < min_trades:
            logger.info(f"LearningEngine: only {len(closed)} trades — skipping (need {min_trades})")
            result = {
                "status": "skipped",
                "reason": f"Not enough trades ({len(closed)} < {min_trades})",
                "trades_analyzed": len(closed),
                "timestamp": datetime.utcnow().isoformat(),
            }
            self._last_result = result
            return result

        adjustments = self._compute_adjustments(closed)
        accuracy_report = self._accuracy_report(closed)
        bias_report = self._bias_report(closed)

        # Persist to DB (best-effort)
        persisted = []
        for adj in adjustments:
            try:
                self._persist_weights(adj, accuracy_report)
                persisted.append(adj.symbol)
            except Exception as e:
                logger.warning(f"Failed to persist weights for {adj.symbol}: {e}")

        result = {
            "status": "completed",
            "trades_analyzed": len(closed),
            "symbols_updated": persisted,
            "accuracy": accuracy_report,
            "bias": bias_report,
            "adjustments": [
                {"symbol": a.symbol, "weights": a.weights,
                 "bias_correction": a.bias_correction}
                for a in adjustments
            ],
            "recommendation": self._recommendation(accuracy_report),
            "timestamp": datetime.utcnow().isoformat(),
        }
        self._last_run = datetime.utcnow()
        self._last_result = result
        logger.info(
            f"LearningEngine completed: {len(closed)} trades, "
            f"accuracy={accuracy_report.get('prediction_accuracy', 0):.2%}, "
            f"symbols updated={persisted}"
        )
        return result

    def get_weights(self, symbol: str) -> Dict[str, float]:
        """
        Return learned weights for *symbol* (falls back to __global__ then defaults).
        Uses in-memory cache with 5-min TTL.
        """
        now = time.monotonic()
        if symbol in self._cache:
            data, ts = self._cache[symbol]
            if now - ts < self._CACHE_TTL:
                return data

        weights = self._load_weights_from_db(symbol)
        self._cache[symbol] = (weights, now)
        return weights

    def get_status(self) -> Dict[str, Any]:
        """Return last run info for the dashboard."""
        return {
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "last_result": self._last_result,
        }

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _load_closed_positions(self, lookback_days: int) -> List[Any]:
        """Load closed positions with indicator data from the last N days."""
        try:
            from init_components import get_components
            pm = get_components().position_manager
            positions = pm.get_closed_positions(limit=500)
            cutoff = datetime.utcnow() - timedelta(days=lookback_days)
            filtered = []
            for p in positions:
                try:
                    et = p.exit_time
                    if isinstance(et, str):
                        et = datetime.fromisoformat(et.replace("Z", "+00:00"))
                    if et and et.replace(tzinfo=None) >= cutoff:
                        filtered.append(p)
                except Exception:
                    filtered.append(p)
            return filtered
        except Exception as e:
            logger.error(f"LearningEngine: could not load closed positions: {e}")
            return []

    def _compute_adjustments(self, closed: List[Any]) -> List[WeightAdjustment]:
        """Compute per-symbol (plus __global__) weight adjustments."""
        by_symbol: Dict[str, List[Any]] = {}
        for pos in closed:
            by_symbol.setdefault(pos.symbol, []).append(pos)
        by_symbol["__global__"] = closed

        adjustments = []
        for symbol, positions in by_symbol.items():
            adj = self._adjust_for_group(symbol, positions)
            if adj:
                adjustments.append(adj)
        return adjustments

    def _adjust_for_group(self, symbol: str, positions: List[Any]) -> Optional[WeightAdjustment]:
        """Compute weight adjustment for a symbol group."""
        with_indicators = [
            p for p in positions
            if (getattr(p, "indicators_at_entry", None) or
                (getattr(p, "pnl_prediction", None) or {}).get("indicators_used"))
        ]
        if len(with_indicators) < 3:
            return None

        winning = [p for p in with_indicators if float(getattr(p, "pnl", 0)) > 0]
        win_rate = len(winning) / len(with_indicators) if with_indicators else 0.5

        # Correlate each indicator with wins
        correlations = self._correlate_indicators(with_indicators)

        # Current defaults
        defaults = {"rsi": 0.15, "macd": 0.10, "bb": 0.10, "ema": 0.10, "volume": 0.05}
        new_weights = {}
        total_signal = sum(max(0, v) for v in correlations.values()) or 1.0

        for ind, corr in correlations.items():
            base = defaults.get(ind, 0.10)
            # Shift weight proportional to correlation, clamped
            delta = corr * 0.05  # max ±5% per cycle
            new_weights[ind] = max(_WEIGHT_MIN, min(_WEIGHT_MAX, base + delta))

        # Bias correction from prediction accuracy
        bias_correction = self._compute_bias_correction(with_indicators)

        return WeightAdjustment(
            symbol=symbol,
            weights=new_weights,
            bias_correction=bias_correction,
            metadata={"win_rate": win_rate, "n": len(with_indicators),
                       "correlations": correlations},
        )

    def _correlate_indicators(self, positions: List[Any]) -> Dict[str, float]:
        """
        For each indicator, compute point-biserial correlation with trade outcome
        (1 = win, 0 = loss).  Returns dict indicator -> correlation [-1..1].
        """
        outcomes = np.array([1.0 if float(getattr(p, "pnl", 0)) > 0 else 0.0
                             for p in positions])

        def _extract(pos, key: str) -> float:
            indicators = (getattr(pos, "indicators_at_entry", None) or
                          (getattr(pos, "pnl_prediction", None) or {}).get("indicators_used") or {})
            if isinstance(indicators, dict):
                v = indicators.get(key, {})
                if isinstance(v, dict):
                    return float(v.get("value", v.get("macd", 0)))
                return float(v) if v is not None else 0.0
            return 0.0

        result = {}
        for ind in ("rsi", "macd", "bb", "ema", "volume"):
            values = np.array([_extract(p, ind) for p in positions])
            if values.std() == 0 or outcomes.std() == 0:
                result[ind] = 0.0
                continue
            corr = float(np.corrcoef(values, outcomes)[0, 1])
            result[ind] = corr if not np.isnan(corr) else 0.0
        return result

    def _compute_bias_correction(self, positions: List[Any]) -> float:
        """
        If predictor systematically over/under-estimates, return a multiplier
        to correct it.  bias_correction = mean(actual/predicted) clamped.
        """
        ratios = []
        for p in positions:
            pred = getattr(p, "pnl_prediction", None)
            if not pred:
                continue
            predicted = float((pred or {}).get("expected_pnl", 0))
            actual = float(getattr(p, "pnl", 0))
            if abs(predicted) > 1e-6:
                ratios.append(actual / predicted)

        if not ratios:
            return 1.0
        median_ratio = float(np.median(ratios))
        return max(_BIAS_CORRECTION_MIN, min(_BIAS_CORRECTION_MAX, median_ratio))

    def _accuracy_report(self, positions: List[Any]) -> Dict[str, Any]:
        """Compute prediction accuracy metrics from closed positions."""
        with_pred = [p for p in positions
                     if getattr(p, "pnl_prediction", None) is not None]
        if not with_pred:
            return {"prediction_accuracy": 0.0, "avg_prediction_error": 0.0,
                    "signal_quality_score": 0.0, "total": 0}

        errors, accurate = [], 0
        for p in with_pred:
            pred_pnl = float((p.pnl_prediction or {}).get("expected_pnl", 0))
            actual_pnl = float(getattr(p, "pnl", 0))
            if abs(pred_pnl) > 1e-6:
                err = abs(actual_pnl - pred_pnl) / abs(pred_pnl)
                errors.append(err)
                if err < 0.20:
                    accurate += 1

        n = len(with_pred)
        avg_err = float(np.mean(errors)) if errors else 0.0
        accuracy = accurate / n if n > 0 else 0.0
        quality = accuracy * 0.7 + (1 - min(avg_err, 1.0)) * 0.3

        return {
            "prediction_accuracy": accuracy,
            "avg_prediction_error": avg_err,
            "signal_quality_score": quality,
            "total": n,
            "accurate": accurate,
        }

    def _bias_report(self, positions: List[Any]) -> Dict[str, Any]:
        """Compute over/under-prediction bias."""
        biases = []
        for p in positions:
            pred = getattr(p, "pnl_prediction", None)
            if not pred:
                continue
            biases.append(float(getattr(p, "pnl", 0)) -
                          float((pred or {}).get("expected_pnl", 0)))

        if not biases:
            return {}

        arr = np.array(biases)
        by_symbol: Dict[str, List[float]] = {}
        for p in positions:
            pred = getattr(p, "pnl_prediction", None)
            if not pred:
                continue
            b = float(getattr(p, "pnl", 0)) - float((pred or {}).get("expected_pnl", 0))
            by_symbol.setdefault(p.symbol, []).append(b)

        return {
            "mean_bias": float(np.mean(arr)),
            "median_bias": float(np.median(arr)),
            "std_bias": float(np.std(arr)),
            "over_prediction_count": int(np.sum(arr < 0)),
            "under_prediction_count": int(np.sum(arr > 0)),
            "by_symbol": {s: round(float(np.mean(v)), 4) for s, v in by_symbol.items()},
        }

    def _recommendation(self, accuracy: Dict) -> str:
        acc = accuracy.get("prediction_accuracy", 0)
        err = accuracy.get("avg_prediction_error", 1)
        q = accuracy.get("signal_quality_score", 0)
        if acc < 0.40:
            return "Accuracy below 40% — indicator weights adjusted. Monitor next 10 trades."
        if err > 0.50:
            return "High prediction error (>50%) — consider adding ATR / volume confirmation."
        if q < 0.50:
            return "Signal quality low — optimizer run recommended."
        return "Predictor performing within acceptable parameters."

    # ── DB persistence ────────────────────────────────────────────────────────

    def _persist_weights(self, adj: WeightAdjustment, accuracy: Dict) -> None:
        if self.db is None:
            return
        w = adj.weights
        meta = {**adj.metadata, "accuracy": accuracy}
        sql = """
        INSERT INTO indicator_weights
            (symbol, rsi_weight, macd_weight, bb_weight, ema_weight, volume_weight,
             bias_correction, trades_analyzed, win_rate, prediction_accuracy,
             avg_prediction_error, signal_quality_score, source, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'learning_engine', %s)
        ON CONFLICT (symbol) DO UPDATE SET
            rsi_weight           = EXCLUDED.rsi_weight,
            macd_weight          = EXCLUDED.macd_weight,
            bb_weight            = EXCLUDED.bb_weight,
            ema_weight           = EXCLUDED.ema_weight,
            volume_weight        = EXCLUDED.volume_weight,
            bias_correction      = EXCLUDED.bias_correction,
            trades_analyzed      = EXCLUDED.trades_analyzed,
            win_rate             = EXCLUDED.win_rate,
            prediction_accuracy  = EXCLUDED.prediction_accuracy,
            avg_prediction_error = EXCLUDED.avg_prediction_error,
            signal_quality_score = EXCLUDED.signal_quality_score,
            source               = 'learning_engine',
            metadata             = EXCLUDED.metadata,
            updated_at           = NOW()
        """
        params = (
            adj.symbol,
            w.get("rsi", 0.15), w.get("macd", 0.10),
            w.get("bb", 0.10), w.get("ema", 0.10), w.get("volume", 0.05),
            adj.bias_correction,
            adj.metadata.get("n", 0),
            adj.metadata.get("win_rate", 0.0),
            accuracy.get("prediction_accuracy", 0.0),
            accuracy.get("avg_prediction_error", 0.0),
            accuracy.get("signal_quality_score", 0.0),
            json.dumps(meta),
        )
        self.db.execute_query(sql, params)

    def _load_weights_from_db(self, symbol: str) -> Dict[str, float]:
        defaults = {"rsi": 0.15, "macd": 0.10, "bb": 0.10,
                    "ema": 0.10, "volume": 0.05, "bias_correction": 1.0}
        if self.db is None:
            return defaults
        try:
            sql = """
            SELECT rsi_weight, macd_weight, bb_weight, ema_weight, volume_weight,
                   bias_correction, updated_at
            FROM indicator_weights
            WHERE symbol = %s
            LIMIT 1
            """
            rows = self.db.execute_query(sql, (symbol,), fetch=True)
            if not rows:
                if symbol != "__global__":
                    return self._load_weights_from_db("__global__")
                return defaults
            row = rows[0]
            # Expire if older than TTL
            updated_at = row[6]
            if updated_at:
                if isinstance(updated_at, str):
                    updated_at = datetime.fromisoformat(updated_at)
                age = (datetime.utcnow() - updated_at.replace(tzinfo=None)).days
                if age > _WEIGHT_TTL_DAYS:
                    logger.info(f"Weights for {symbol} expired ({age}d old) — using defaults")
                    return defaults
            return {
                "rsi": row[0], "macd": row[1], "bb": row[2],
                "ema": row[3], "volume": row[4], "bias_correction": row[5],
            }
        except Exception as e:
            logger.warning(f"Could not load weights for {symbol}: {e}")
            return defaults


# Module-level singleton
_engine: Optional[LearningEngine] = None


def get_learning_engine() -> LearningEngine:
    global _engine
    if _engine is None:
        _engine = LearningEngine()
    return _engine

"""Hybrid suggester that combines QIO, swarm, ML and capital allocation."""
from typing import Dict, List, Optional
from .quantum_space import qio_refine_best
from .signal_swarm import SignalSwarm
from .ml_predictor import LightweightEnsemble, ThompsonBandit
from .capital_allocator import allocate_spot_futures


# Mirror of backtest optimizer parameter space
PARAM_SPACE = {
    'kelly_base_usdt': (50.0, 200.0),
    'kelly_max_usdt': (200.0, 800.0),
    'kelly_max_pct_liquid': (0.25, 0.75),
    'futures_kelly_max_usdt': (300.0, 1200.0),
    'min_strength': (0.35, 0.65),
    'min_prob': (0.48, 0.70),
    'vol_spike_min': (0.8, 2.5),
    'spot_sl_pct': (0.010, 0.035),
    'spot_tp_pct': (0.018, 0.065),
    'futures_sl_pct': (0.012, 0.040),
    'futures_tp_pct': (0.020, 0.080),
}

# Safety bounds for live application
SAFE_BOUNDS = {
    'kelly_base_usdt': (50.0, 300.0),
    'kelly_max_usdt': (200.0, 1200.0),
    'kelly_max_pct_liquid': (0.20, 0.90),
    'futures_kelly_max_usdt': (300.0, 2000.0),
    'min_strength': (0.40, 0.90),
    'min_prob': (0.45, 0.80),
    'vol_spike_min': (1.0, 3.0),
    'spot_sl_pct': (0.010, 0.040),
    'spot_tp_pct': (0.018, 0.080),
    'futures_sl_pct': (0.012, 0.050),
    'futures_tp_pct': (0.020, 0.100),
}


def _safe_clip(cfg: Dict) -> Dict:
    out = {}
    for k, (lo, hi) in SAFE_BOUNDS.items():
        out[k] = max(lo, min(hi, cfg.get(k, (lo + hi) / 2)))
    return out


class HybridSuggester:
    """
    Main entry point for the hybrid optimizer framework.
    Given a list of backtest runs, it:
      1. Refines top candidates with SQA (quantum-inspired).
      2. Validates configs with the swarm.
      3. Predicts PnL with a lightweight ensemble.
      4. Allocates capital between SPOT and FUTURES.
    """

    def __init__(self, settings=None):
        self.settings = settings
        self.swarm = SignalSwarm(settings)
        self.ensemble = LightweightEnsemble()

    def recommend(
        self,
        top_runs: List[Dict],
        regime: str,
        spot_pnl_7d: float = 0,
        futures_pnl_7d: float = 0,
    ) -> Dict:
        if not top_runs:
            return {"ok": False, "error": "No backtest runs provided"}

        # Build evaluator from top runs (PnL surrogate)
        run_lookup = {r["label"]: r for r in top_runs}

        def _eval_cfg(cfg: Dict) -> float:
            # Find nearest run in top_runs by L2 distance in normalized space
            best_score = float('-inf')
            for r in top_runs:
                rp = r.get("params", {})
                dist = 0.0
                for k in PARAM_SPACE:
                    lo, hi = PARAM_SPACE[k]
                    norm = (hi - lo) or 1
                    d = (cfg.get(k, float(rp.get(k, 0))) - float(rp.get(k, 0))) / norm
                    dist += d * d
                score = float(r.get("pnl_total", 0)) - dist * 100
                if score > best_score:
                    best_score = score
            return best_score

        # Run SQA on top runs
        top_configs = [r.get("params", {}) for r in top_runs[:10]]
        qio_result = qio_refine_best(PARAM_SPACE, _eval_cfg, top_configs, seed=42)
        refined_cfg = _safe_clip(qio_result["best_config"])

        # Swarm evaluation
        swarm_eval = self.swarm.evaluate_config(refined_cfg, regime, recent_pnl=spot_pnl_7d + futures_pnl_7d)

        # Ensemble prediction
        predicted_pnl = self.ensemble.predict_pnl(refined_cfg)

        # Capital allocation
        allocation = allocate_spot_futures(regime, spot_pnl_7d, futures_pnl_7d)

        # Build patch suggestion (similar to /backtest/best)
        patch = {
            "kelly_base_usdt": round(refined_cfg["kelly_base_usdt"], 0),
            "kelly_max_usdt": round(refined_cfg["kelly_max_usdt"], 0),
            "kelly_max_pct_liquid": round(refined_cfg["kelly_max_pct_liquid"], 3),
            "futures_kelly_max_usdt": round(refined_cfg["futures_kelly_max_usdt"], 0),
            "stop_loss_pct": round(refined_cfg["spot_sl_pct"] * 100, 4),
            "take_profit_pct": round(refined_cfg["spot_tp_pct"] * 100, 4),
            "futures_sl_pct": round(refined_cfg["futures_sl_pct"] * 100, 4),
            "futures_tp_pct": round(refined_cfg["futures_tp_pct"] * 100, 4),
            "min_signal_strength": round(max(0.40, refined_cfg["min_strength"]), 3),
            "vol_spike_min": round(max(1.0, refined_cfg["vol_spike_min"]), 2),
        }

        category_patch = {
            "LARGE_CAP": {"min_prob": round(refined_cfg["min_prob"], 4),
                          "take_profit_pct": round(refined_cfg["spot_tp_pct"] * 100, 4)},
            "MIDCAP_ALT": {"min_prob": round(refined_cfg["min_prob"], 4),
                           "take_profit_pct": round(refined_cfg["spot_tp_pct"] * 100, 4)},
            "NARRATIVE": {"min_prob": round(refined_cfg["min_prob"], 4),
                          "take_profit_pct": round(refined_cfg["spot_tp_pct"] * 100, 4)},
            "MEME": {"min_prob": round(refined_cfg["min_prob"], 4),
                     "take_profit_pct": round(refined_cfg["spot_tp_pct"] * 100, 4)},
        }

        futures_category_patch = {
            "LARGE_CAP": {"min_prob": round(refined_cfg["min_prob"], 4),
                          "stop_loss_pct": round(refined_cfg["futures_sl_pct"] * 100, 4),
                          "take_profit_pct": round(refined_cfg["futures_tp_pct"] * 100, 4)},
            "MIDCAP": {"min_prob": round(refined_cfg["min_prob"], 4),
                       "stop_loss_pct": round(refined_cfg["futures_sl_pct"] * 100, 4),
                       "take_profit_pct": round(refined_cfg["futures_tp_pct"] * 100, 4)},
            "NARRATIVE": {"min_prob": round(refined_cfg["min_prob"], 4),
                          "stop_loss_pct": round(refined_cfg["futures_sl_pct"] * 100, 4),
                          "take_profit_pct": round(refined_cfg["futures_tp_pct"] * 100, 4)},
        }

        confidence = round(swarm_eval["consensus"] * 0.5 + min(1.0, max(0.0, predicted_pnl / 50)) * 0.5, 4)

        return {
            "ok": True,
            "source": "qio+swarm+ml",
            "confidence": confidence,
            "refined_config": refined_cfg,
            "swarm_evaluation": swarm_eval,
            "predicted_pnl": predicted_pnl,
            "capital_allocation": allocation,
            "patch": patch,
            "category_patch": category_patch,
            "futures_category_patch": futures_category_patch,
            "safe": all(SAFE_BOUNDS[k][0] <= refined_cfg[k] <= SAFE_BOUNDS[k][1] for k in SAFE_BOUNDS),
        }


def recommend_config(
    top_runs: List[Dict],
    regime: str,
    spot_pnl_7d: float = 0,
    futures_pnl_7d: float = 0,
    settings=None,
) -> Dict:
    """Convenience function to get a recommendation without instantiating the class."""
    suggester = HybridSuggester(settings)
    return suggester.recommend(top_runs, regime, spot_pnl_7d, futures_pnl_7d)

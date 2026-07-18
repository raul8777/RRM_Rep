#!/usr/bin/env python3
"""Calibrate per-regime logistic posterior from the shadow log."""
import json
import math
import collections
import os
from typing import Dict, List, Tuple
from scipy.optimize import minimize

SHADOW_LOG = "/app/Dev/benchmarks/forced_regime_sweep/evidence_gate_shadow_log.jsonl"
OUTPUT_DIR = "/app/Dev/benchmarks/forced_regime_sweep/calibration"


def logit(p: float) -> float:
    p = max(1e-6, min(1 - 1e-6, float(p)))
    return math.log(p / (1 - p))


def extract_features(rec: dict) -> Tuple[float, float]:
    meta = rec["entry_metadata"]
    prob = float(meta.get("predictor_prob") or 0.5)
    ev = float(meta.get("predictor_ev") or 0.0)
    return logit(prob), ev


def fit_logistic(rows: List[dict], l2: float = 0.5) -> Dict[str, float]:
    # rows: list of dicts with 'x' (feature list), 'y' (0/1)
    X = [r["x"] for r in rows]
    y = [r["y"] for r in rows]
    n_pos = sum(y)
    n_neg = len(y) - n_pos
    # Balanced sample weights
    pos_w = 0.5 / n_pos if n_pos else 1.0
    neg_w = 0.5 / n_neg if n_neg else 1.0

    def sigmoid(z):
        return 1.0 / (1.0 + math.exp(-max(min(z, 20), -20)))

    def loss(w):
        b0, b1, b2 = w
        nll = 0.0
        for xi, yi in zip(X, y):
            z = b0 + b1 * xi[0] + b2 * xi[1]
            p = sigmoid(z)
            wgt = pos_w if yi else neg_w
            # stable log loss
            nll += wgt * (-(yi * math.log(max(p, 1e-9)) + (1 - yi) * math.log(max(1 - p, 1e-9))))
        nll += 0.5 * l2 * sum(wi * wi for wi in w)
        return nll

    res = minimize(loss, [0.0, 1.0, 0.0], method="Nelder-Mead", options={"maxiter": 5000, "xatol": 1e-7})
    return {
        "intercept": float(res.x[0]),
        "logit_coef": float(res.x[1]),
        "ev_coef": float(res.x[2]),
        "success": bool(res.success),
        "n": len(rows),
        "n_pos": int(n_pos),
    }


def evaluate_threshold(rows: List[dict], coefs: Dict, threshold: float):
    b0 = coefs["intercept"]
    b1 = coefs["logit_coef"]
    b2 = coefs["ev_coef"]
    selected = []
    for r in rows:
        x1, x2 = r["x"]
        z = b0 + b1 * x1 + b2 * x2
        p = 1.0 / (1.0 + math.exp(-max(min(z, 20), -20)))
        if p >= threshold:
            selected.append(r)
    if not selected:
        return None
    wins = sum(r["y"] for r in selected)
    mean_pnl = sum(r["pnl"] for r in selected) / len(selected)
    return {
        "threshold": threshold,
        "n": len(selected),
        "winrate": wins / len(selected),
        "mean_pnl": mean_pnl,
    }


def main():
    recs = [json.loads(l) for l in open(SHADOW_LOG) if l.strip()]
    by_reg = collections.defaultdict(list)
    for r in recs:
        prob = (r["entry_metadata"] or {}).get("predictor_prob")
        if prob is None:
            continue
        x1, x2 = extract_features(r)
        by_reg[r["forced_regime"]].append({
            "x": [x1, x2],
            "y": 1 if r["pnl_usdt"] > 0 else 0,
            "pnl": r["pnl_usdt"],
        })

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    calibration = {}
    thresholds = {}
    print(f"Loaded {len(recs)} shadow records")
    for reg, rows in by_reg.items():
        if len(rows) < 50:
            print(f"Skipping {reg}: only {len(rows)} records")
            continue
        coefs = fit_logistic(rows)
        print(f"\n{reg}: n={coefs['n']} pos={coefs['n_pos']} {coefs}")
        # grid search threshold to maximize mean_pnl with positive mean
        best = None
        for thr in [round(x * 0.05, 2) for x in range(1, 20)]:
            ev = evaluate_threshold(rows, coefs, thr)
            if ev is None:
                continue
            if ev["mean_pnl"] > 0 and (best is None or ev["mean_pnl"] > best["mean_pnl"]):
                best = ev
        # fallback: threshold that gives best mean_pnl even if negative
        if best is None:
            for thr in [round(x * 0.05, 2) for x in range(1, 20)]:
                ev = evaluate_threshold(rows, coefs, thr)
                if ev is None:
                    continue
                if best is None or ev["mean_pnl"] > best["mean_pnl"]:
                    best = ev
        print(f"  selected threshold={best['threshold'] if best else None}")
        if best:
            print(f"    n={best['n']} winrate={best['winrate']:.3f} mean_pnl={best['mean_pnl']:.3f}")
            thresholds[reg] = best["threshold"]
        calibration[reg] = coefs

    result = {"posterior_model": calibration, "thresholds": thresholds}
    out_path = os.path.join(OUTPUT_DIR, "evidence_gate_calibration.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nCalibration written to {out_path}")


if __name__ == "__main__":
    main()

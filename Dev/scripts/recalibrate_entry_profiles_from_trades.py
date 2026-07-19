"""Recalibrate entry_profile_gate thresholds from real closed trades.

Connects to the production/trading database, reads recently closed trades, and
compares the realized win-rate / mean return of each regime+profile bucket
against the targets defined in the plan. It prints a diagnostic report and,
when called with --apply, writes an adjusted thresholds JSON file.

Usage:
    python Dev/scripts/recalibrate_entry_profiles_from_trades.py --days 30
    python Dev/scripts/recalibrate_entry_profiles_from_trades.py --days 60 --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2

REPO_ROOT = Path(__file__).resolve().parents[2]
THRESHOLDS_PATH = REPO_ROOT / "Dev" / "benchmarks" / "entry_profile_calibration" / "entry_profile_thresholds.json"


def _parse_dict_blob(blob: Any) -> Dict[str, Any]:
    if blob is None:
        return {}
    if isinstance(blob, dict):
        return blob
    try:
        return json.loads(str(blob))
    except Exception:
        return {}


def _extract_expected_return_pct(metadata: Dict[str, Any], pnl_prediction: Dict[str, Any]) -> Optional[float]:
    """Best effort extraction of the gate score (expected return %) used at entry."""
    # Direct percentage fields from pnl_service
    for key in ("expected_pnl_percentage", "expected_pnl_pct", "expected_return_pct"):
        if key in pnl_prediction:
            return float(pnl_prediction[key])
    # Legacy metadata fields
    for key in ("entry_pred_pnl_pct", "expected_pnl_percentage"):
        if key in metadata:
            return float(metadata[key])
    # Infer from USDT PnL and position size
    pnl = pnl_prediction.get("expected_pnl") or metadata.get("entry_pred_pnl")
    size = pnl_prediction.get("position_size_usdt") or metadata.get("entry_position_size_usdt")
    if pnl is not None and size:
        try:
            size_f = float(size)
            if size_f > 0:
                return float(pnl) / size_f * 100.0
        except Exception:
            pass
    return None


def _extract_profile(metadata: Dict[str, Any], pnl_prediction: Dict[str, Any]) -> Optional[str]:
    for key in ("profile_label", "peak_quality_profile", "entry_profile"):
        if key in metadata:
            return str(metadata[key]).upper() or None
    for key in ("profile_label", "peak_quality_profile"):
        if key in pnl_prediction:
            return str(pnl_prediction[key]).upper() or None
    return None


def _extract_regime(metadata: Dict[str, Any]) -> Optional[str]:
    for key in ("regime_mode", "regime_at_entry", "regime"):
        if key in metadata:
            return str(metadata[key]).upper() or None
    return None


def _load_thresholds(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_thresholds(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Backup existing file
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _fetch_trades(days: int) -> List[Dict[str, Any]]:
    url = os.environ.get("DB_URL", "postgresql://crypto_agent:crypto_agent_password@postgres:5432/crypto_agent")
    conn = psycopg2.connect(url)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT symbol, side, entry_price, exit_price, quantity, pnl, pnl_percentage,
                   entry_time, exit_time, close_reason, metadata, pnl_prediction,
                   indicators_at_entry, prediction_accuracy
            FROM trades_closed
            WHERE exit_time >= NOW() - INTERVAL '%s days'
            ORDER BY exit_time DESC
            """,
            (days,),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    return rows


def _analyse(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    buckets: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        meta = _parse_dict_blob(r.get("metadata"))
        pred = _parse_dict_blob(r.get("pnl_prediction"))
        regime = _extract_regime(meta) or "UNKNOWN"
        profile = _extract_profile(meta, pred)
        if profile not in ("A", "B", "C"):
            continue
        expected_pct = _extract_expected_return_pct(meta, pred)
        try:
            pnl_pct = float(r.get("pnl_percentage") or 0)
        except Exception:
            pnl_pct = 0.0
        buckets[(regime, profile)].append({
            "symbol": r.get("symbol"),
            "expected_pct": expected_pct,
            "pnl_pct": pnl_pct,
            "pnl": r.get("pnl"),
            "entry_time": r.get("entry_time"),
            "exit_time": r.get("exit_time"),
        })

    summary: Dict[str, Any] = {"total_trades": len(rows), "buckets": {}, "generated_at": datetime.now(timezone.utc).isoformat()}
    for (regime, profile), items in sorted(buckets.items()):
        pnls = [x["pnl_pct"] for x in items]
        wins = sum(1 for p in pnls if p > 0)
        n = len(items)
        win_rate = wins / n if n else 0.0
        avg_expected = sum(e for e in (x["expected_pct"] for x in items) if e is not None) / n if n else 0.0
        summary["buckets"][f"{regime}_{profile}"] = {
            "n": n,
            "win_rate": round(win_rate, 4),
            "mean_pnl_pct": round(sum(pnls) / n, 4) if n else 0.0,
            "total_pnl": round(sum(float(x["pnl"] or 0) for x in items), 4),
            "mean_expected_pnl_pct": round(avg_expected, 4),
        }
    return summary


def _suggest_threshold_adjustments(thresholds: Dict[str, Any], summary: Dict[str, Any]) -> Dict[str, Any]:
    """Return proposed threshold deltas without mutating the original thresholds."""
    proposed: Dict[str, Dict[str, float]] = {}
    current = thresholds.get("thresholds", {})
    for bucket_key, stats in summary["buckets"].items():
        regime, profile = bucket_key.rsplit("_", 1)
        if regime not in current:
            continue
        current_thr = current[regime].get(profile)
        if current_thr is None:
            continue
        n = stats["n"]
        wr = stats["win_rate"]
        mean_pnl = stats["mean_pnl_pct"]

        delta = 0.0
        reason = "ok"
        if profile == "A":
            if n >= 20 and wr < 0.85:
                delta = current_thr * 0.10
                reason = f"A win_rate {wr:.2%} < 85%, raise threshold 10%"
            elif n >= 20 and mean_pnl < 0.005:
                delta = current_thr * 0.10
                reason = f"A mean_pnl {mean_pnl:.4%} < 0.5%, raise threshold 10%"
        elif profile == "B":
            if n >= 30 and wr < 0.80:
                delta = current_thr * 0.07
                reason = f"B win_rate {wr:.2%} < 80%, raise threshold 7%"
        elif profile == "C":
            if n >= 50 and wr < 0.65:
                delta = current_thr * 0.10
                reason = f"C win_rate {wr:.2%} < 65%, raise threshold 10%"
            elif n >= 50 and mean_pnl <= 0.0:
                delta = current_thr * 0.10
                reason = f"C mean_pnl {mean_pnl:.4%} <= 0, raise threshold 10%"

        if delta != 0.0:
            proposed.setdefault(regime, {})[profile] = round(current_thr + delta, 6)
            proposed.setdefault(regime, {})[f"{profile}_reason"] = reason
    return proposed


def _apply_adjustments(thresholds: Dict[str, Any], proposed: Dict[str, Dict[str, float]]) -> Dict[str, Any]:
    new_thresholds = json.loads(json.dumps(thresholds))
    for regime, changes in proposed.items():
        if regime not in new_thresholds.setdefault("thresholds", {}):
            continue
        for profile, new_value in changes.items():
            if profile.endswith("_reason"):
                continue
            new_thresholds["thresholds"][regime][profile] = new_value
    return new_thresholds


def main() -> int:
    parser = argparse.ArgumentParser(description="Recalibrate entry_profile_gate from real trades")
    parser.add_argument("--days", type=int, default=30, help="Lookback window in days")
    parser.add_argument("--apply", action="store_true", help="Write adjusted thresholds to disk")
    parser.add_argument("--thresholds-path", type=Path, default=THRESHOLDS_PATH)
    args = parser.parse_args()

    rows = _fetch_trades(args.days)
    summary = _analyse(rows)
    thresholds = _load_thresholds(args.thresholds_path)
    proposed = _suggest_threshold_adjustments(thresholds, summary)

    report = {
        "lookback_days": args.days,
        **summary,
        "proposed_adjustments": proposed,
        "thresholds_path": str(args.thresholds_path),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))

    if args.apply and proposed:
        new_thresholds = _apply_adjustments(thresholds, proposed)
        _save_thresholds(args.thresholds_path, new_thresholds)
        print(f"\nUpdated thresholds written to {args.thresholds_path}; backup saved as .bak")
    elif args.apply:
        print("\n--apply requested but no adjustments were proposed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""tmp_lifecycle_multi_window.py — lifecycle validation across 3 windows and profiles A/B/C."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime
from typing import Dict, List


class _ResultEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj)):
            return "inf" if obj > 0 else "-inf"
        return super().default(obj)

repo_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, repo_root)

from config import get_settings
from simulation.mimic_lifecycle_adapter import run_lifecycle_simulation

WINDOWS_DIR = os.path.join(repo_root, "Dev", "benchmarks", "windows")
WINDOW_IDS = [
    "win_20260415_20260430",  # A best overall
    "win_20260515_20260531",  # A worst / B-C rescue
    "win_20260317_20260331",  # different regime
]
DATA_DIR = os.path.join(repo_root, "historical_data")
PROFILES = ["A", "B", "C"]


def _load_window(window_id: str):
    path = os.path.join(WINDOWS_DIR, f"{window_id}.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _metrics(res: dict):
    results = res.get("results", [])
    total_value = results[-1]["total_value"] if results else res.get("initial_usdt", 2910.0)
    initial = 2910.0
    pnl_usdt = total_value - initial
    pnl_pct = (pnl_usdt / initial) * 100 if initial else 0.0

    entries = 0
    exits = 0
    exit_reasons: Dict[str, int] = {}
    realized_pnl_usdt = 0.0
    win_count = 0
    loss_count = 0
    max_value = initial
    max_dd = 0.0
    for r in results:
        val = r.get("total_value", initial)
        max_value = max(max_value, val)
        dd = (max_value - val) / max_value * 100
        max_dd = max(max_dd, dd)
        s = r.get("summary", {})
        entries += len(s.get("entries", []))
        exits += len(s.get("exits", []))
        for reason, count in s.get("exit_reasons", {}).items():
            exit_reasons[reason] = exit_reasons.get(reason, 0) + count
        realized_pnl_usdt += s.get("realized_pnl_usdt", 0.0)
        win_count += s.get("win_count", 0)
        loss_count += s.get("loss_count", 0)

    total_losses = abs(sum(
        e.get("realized_pnl_usdt", 0.0)
        for r in results
        for e in r.get("summary", {}).get("exits", [])
        if e.get("realized_pnl_usdt", 0.0) < 0
    ))
    total_wins = sum(
        e.get("realized_pnl_usdt", 0.0)
        for r in results
        for e in r.get("summary", {}).get("exits", [])
        if e.get("realized_pnl_usdt", 0.0) > 0
    )
    profit_factor: Optional[float] = None
    if total_losses > 0:
        profit_factor = total_wins / total_losses
    elif total_wins > 0:
        profit_factor = float("inf")

    def _safe_float(value):
        if value is None:
            return None
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        if math.isnan(value):
            return "nan"
        return value

    return {
        "pnl_usdt": pnl_usdt,
        "pnl_pct": pnl_pct,
        "entries": entries,
        "exits": exits,
        "exit_reasons": exit_reasons,
        "realized_pnl_usdt": round(realized_pnl_usdt, 4),
        "win_count": win_count,
        "loss_count": loss_count,
        "profit_factor": _safe_float(profit_factor),
        "max_drawdown_pct": max_dd,
        "final_value": total_value,
        "cycles": len(results),
    }


def _run_profile(settings, profile: str) -> List[dict]:
    print(f"\n{'='*60}")
    print(f"[lifecycle] PROFILE {profile}")
    print(f"{'='*60}")
    summaries = []
    for window_id in WINDOW_IDS:
        print(f"\n[lifecycle] running {window_id} ...")
        w = _load_window(window_id)
        available = [s for s in w.get("symbols", []) if os.path.exists(os.path.join(DATA_DIR, f"{s}_1h.parquet"))]
        print(f"[lifecycle] {window_id}: {len(available)}/{len(w.get('symbols', []))} symbols have data")
        res = run_lifecycle_simulation(
            settings=settings,
            symbols=available,
            interval="1h",
            start=datetime.fromisoformat(w["start"]),
            end=datetime.fromisoformat(w["end"]),
            initial_usdt=2910.0,
            forced_profile=profile,
            lifecycle_config=None,
            profile_overrides=None,
            max_cycles=100,
            data_dir=DATA_DIR,
        )
        m = _metrics(res)
        record = {"window": window_id, **m}
        summaries.append(record)
        print(json.dumps(record, indent=2, ensure_ascii=False))
    return summaries


def main() -> int:
    parser = argparse.ArgumentParser(description="Lifecycle multi-window validation")
    parser.add_argument("--profile", choices=PROFILES + ["all"], default="all", help="Profile to run")
    args = parser.parse_args()

    os.environ["BINANCE_API_KEY"] = "dummy"
    os.environ["BINANCE_API_SECRET"] = "dummy"
    os.environ["TRADINGVIEW_WEBHOOK_SECRET"] = "dummy"
    os.environ["PYTHONPATH"] = repo_root

    settings = get_settings()
    settings.dry_run = True
    settings.allow_real_trading = False

    profiles = PROFILES if args.profile == "all" else [args.profile]
    all_summaries: Dict[str, List[dict]] = {}
    for profile in profiles:
        summaries = _run_profile(settings, profile)
        all_summaries[profile] = summaries
        out_path = os.path.join(repo_root, "Dev", "benchmarks", f"lifecycle_multi_window_profile_{profile}_20260713.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(summaries, f, cls=_ResultEncoder, indent=2, ensure_ascii=False)
        print(f"\n[lifecycle] saved {out_path}")

    print("\n" + "=" * 60)
    print("CROSS-PROFILE SUMMARY")
    print("=" * 60)
    rows = []
    for profile, summaries in all_summaries.items():
        pf_values = [s["profit_factor"] for s in summaries]
        if any(isinstance(v, str) for v in pf_values):
            avg_pf = "inf" if any(v == "inf" for v in pf_values) else None
        else:
            avg_pf = round(sum(pf_values) / len(pf_values), 4)
        rows.append({
            "profile": profile,
            "avg_pnl_pct": round(sum(s["pnl_pct"] for s in summaries) / len(summaries), 2),
            "total_entries": sum(s["entries"] for s in summaries),
            "total_exits": sum(s["exits"] for s in summaries),
            "realized_pnl_usdt": round(sum(s["realized_pnl_usdt"] for s in summaries), 2),
            "win_count": sum(s["win_count"] for s in summaries),
            "loss_count": sum(s["loss_count"] for s in summaries),
            "avg_profit_factor": avg_pf,
            "avg_dd_pct": round(sum(s["max_drawdown_pct"] for s in summaries) / len(summaries), 2),
        })
    print(json.dumps(rows, cls=_ResultEncoder, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

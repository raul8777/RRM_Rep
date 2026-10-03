"""Horizon counterfactual analysis for Super MIMIC entry results.

Replays every recorded entry against historical 1h bars and evaluates exits at
fixed holding horizons plus first-touch SL/TP using contract prices. This turns
the 14-day window-end liquidation results into a proper 2-4h evaluation.

Usage:
    python Dev/scripts/mimic_horizon_counterfactual.py --workers 8
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = os.path.join(repo_root, "historical_data")
RESULTS_GLOBS = [
    "Dev/benchmarks/super_mimic/sm_20260922_013215_23f9f6/mimic_sweep_results.jsonl",
    "Dev/benchmarks/super_mimic/sm_20260919_133754_a0d7d7/mimic_sweep_results.jsonl",
    "Dev/benchmarks/super_mimic/sm_20260919_060423_3593dc/mimic_sweep_results.jsonl",
]
HORIZONS_H = [1, 2, 4, 8, 24]
COMMISSION_RATE = 0.0004  # taker both sides, applied on notional


def _ts(v: Any) -> pd.Timestamp:
    return pd.to_datetime(v, utc=True)


def _collect_entries(path: str) -> List[Dict[str, Any]]:
    entries = []
    if not os.path.exists(path):
        return entries
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            window_id = row.get("window_id")
            profile = row.get("profile")
            params = row.get("params") or {}
            for item in (row.get("e2e_evidence") or {}).get("lineage") or []:
                contract = item.get("selected_contract") or {}
                symbol = item.get("symbol")
                ts = item.get("timestamp")
                side = str(item.get("selected_side") or contract.get("side") or "").upper()
                entry = float(contract.get("entry_price") or item.get("entry_price") or 0)
                stop = contract.get("effective_stop_price")
                target = contract.get("effective_target_price")
                if not symbol or not ts or side not in ("LONG", "SHORT") or entry <= 0:
                    continue
                entries.append({
                    "experiment": os.path.basename(os.path.dirname(path)),
                    "window_id": window_id,
                    "profile": profile,
                    "symbol": symbol,
                    "side": side,
                    "timestamp": ts,
                    "position_id": item.get("position_id"),
                    "entry_price": entry,
                    "stop_price": float(stop) if stop else None,
                    "target_price": float(target) if target else None,
                    "qty": float(contract.get("qty") or item.get("quantity") or 0.0),
                    "notional": float(contract.get("notional_usdt") or 0.0),
                    "expected_net": contract.get("expected_net_pnl_usdt"),
                    "params_key": json.dumps(params, sort_keys=True, default=str),
                })
    return entries


def _load_bars(symbol: str) -> pd.DataFrame:
    path = os.path.join(DATA_DIR, f"{symbol}_1h.parquet")
    frame = pd.read_parquet(path, columns=["open_time", "high", "low", "close"])
    frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
    return frame.sort_values("open_time").reset_index(drop=True)


def _evaluate_entry(entry: Dict[str, Any], bars: pd.DataFrame) -> Optional[Dict[str, Any]]:
    start_ts = _ts(entry["timestamp"])
    future = bars[bars["open_time"] > start_ts]
    if future.empty:
        return None
    is_short = entry["side"] == "SHORT"
    entry_price = entry["entry_price"]
    qty = entry["qty"]
    stop = entry["stop_price"]
    target = entry["target_price"]

    def pnl_at(price: float) -> float:
        gross = qty * (entry_price - price if is_short else price - entry_price)
        return gross - COMMISSION_RATE * (entry_price + price) * qty

    # First-touch SL/TP path (conservative: SL wins inside a shared bar)
    path_rows = []
    hit_reason = None
    hit_price = None
    hit_time = None
    for _, bar in future.iterrows():
        high, low = float(bar["high"]), float(bar["low"])
        if is_short:
            if stop and high >= stop:
                hit_reason, hit_price = "SL", stop
            elif target and low <= target:
                hit_reason, hit_price = "TP", target
        else:
            if stop and low <= stop:
                hit_reason, hit_price = "SL", stop
            elif target and high >= target:
                hit_reason, hit_price = "TP", target
        if hit_reason:
            hit_time = bar["open_time"]
            break
        path_rows.append(bar)
        if len(path_rows) > max(HORIZONS_H) + 48:
            break

    horizon_results = {}
    for h in HORIZONS_H:
        boundary = start_ts + pd.Timedelta(hours=h)
        slice_ = future[future["open_time"] <= boundary]
        if slice_.empty:
            continue
        # Did SL/TP hit inside the horizon? conservative SL-first.
        reason = None
        exit_price = float(slice_.iloc[-1]["close"])
        for _, bar in slice_.iterrows():
            high, low = float(bar["high"]), float(bar["low"])
            if is_short:
                if stop and high >= stop:
                    reason, exit_price = "SL", stop
                    break
                if target and low <= target:
                    reason, exit_price = "TP", target
                    break
            else:
                if stop and low <= stop:
                    reason, exit_price = "SL", stop
                    break
                if target and high >= target:
                    reason, exit_price = "TP", target
                    break
        horizon_results[h] = {
            "pnl": pnl_at(exit_price),
            "exit": reason or "TIME",
        }

    resolved_hours = (
        (hit_time - start_ts).total_seconds() / 3600.0 if hit_time is not None else None
    )
    return {
        **{k: entry[k] for k in ("experiment", "window_id", "profile", "symbol", "side", "timestamp", "position_id", "entry_price", "expected_net", "params_key")},
        "hit_reason": hit_reason or "UNRESOLVED",
        "resolved_hours": resolved_hours,
        "hit_pnl": pnl_at(hit_price) if hit_price else None,
        "horizons": horizon_results,
    }


def _worker(args):
    symbol, entries = args
    try:
        bars = _load_bars(symbol)
    except Exception:
        return []
    return [r for r in (_evaluate_entry(e, bars) for e in entries) if r]


def _summarize(rows: List[Dict[str, Any]], key_fn) -> Dict[str, Any]:
    import statistics
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(key_fn(r), []).append(r)
    out = {}
    for label, items in sorted(groups.items()):
        summary = {"n": len(items)}
        resolved = [i["resolved_hours"] for i in items if i["resolved_hours"] is not None]
        if resolved:
            summary["median_hours_to_sltp"] = round(statistics.median(resolved), 2)
            summary["pct_resolved"] = round(len(resolved) / len(items) * 100, 1)
        hit_pnls = [i["hit_pnl"] for i in items if i["hit_pnl"] is not None]
        if hit_pnls:
            summary["sltp_total_pnl"] = round(sum(hit_pnls), 2)
            summary["sltp_win_rate"] = round(sum(p > 0 for p in hit_pnls) / len(hit_pnls) * 100, 1)
        for h in HORIZONS_H:
            vals = [i["horizons"][h]["pnl"] for i in items if h in i["horizons"]]
            if vals:
                wins = [v for v in vals if v > 0]
                losses = [v for v in vals if v < 0]
                pf = (sum(wins) / abs(sum(losses))) if losses else None
                summary[f"h{h}_mean_pnl"] = round(statistics.mean(vals), 3)
                summary[f"h{h}_win_rate"] = round(len(wins) / len(vals) * 100, 1)
                summary[f"h{h}_pf"] = round(pf, 3) if pf is not None else None
        out[label] = summary
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", default=os.path.join(repo_root, "Dev", "benchmarks", "horizon_counterfactual.json"))
    args = parser.parse_args()

    entries = []
    for rel in RESULTS_GLOBS:
        entries.extend(_collect_entries(os.path.join(repo_root, rel)))
    print(f"entries collected: {len(entries)}")

    by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for e in entries:
        by_symbol.setdefault(e["symbol"], []).append(e)

    tasks = [(s, es) for s, es in sorted(by_symbol.items())]
    results: List[Dict[str, Any]] = []
    if args.workers > 1:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for chunk in pool.map(_worker, tasks):
                results.extend(chunk)
    else:
        for t in tasks:
            results.extend(_worker(t))
    print(f"evaluated: {len(results)}")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entries": len(results),
        "by_side": _summarize(results, lambda r: r["side"]),
        "by_window": _summarize(results, lambda r: r["window_id"]),
        "by_side_window": _summarize(results, lambda r: f'{r["window_id"]}|{r["side"]}'),
        "overall": _summarize(results, lambda r: "ALL")["ALL"],
        "resolution_reasons": {},
        "dedup_by_trade": {},
    }
    from collections import Counter
    report["resolution_reasons"] = dict(Counter(r["hit_reason"] for r in results))

    # Deduped view: same symbol+timestamp+side across jobs counted once
    dedup: Dict[str, Dict[str, Any]] = {}
    for r in results:
        dedup.setdefault(f'{r["symbol"]}|{r["side"]}|{r.get("timestamp")}', r)
    dedup_rows = list(dedup.values())
    report["dedup_by_trade"] = {
        "n": len(dedup_rows),
        "by_side": _summarize(dedup_rows, lambda r: r["side"]),
        "overall": _summarize(dedup_rows, lambda r: "ALL")["ALL"],
    }

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
    print(json.dumps(report["overall"], indent=2))
    print(json.dumps(report["by_side"], indent=2))
    print(f"report: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

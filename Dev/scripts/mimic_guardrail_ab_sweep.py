"""
MIMIC guardrail USD — A/B sweep sobre ventanas históricas.

Corre el ciclo de producción completo (run_trades.execute_once) contra velas
históricas una vez con la configuración baseline y otra con el guardrail USD,
guardando métricas comparativas y per-trade PnL para validar la cola de pérdidas.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root))

from config import get_settings
from simulation.mimic_runner import SimulationRunner


WINDOWS_DIR = repo_root / "Dev" / "benchmarks" / "windows"

MODES = ["SPOT_BULL", "RANGE_SCALP", "PRESERVATION", "FUTURES_SHORT"]
PROFILES = ["A", "B", "C"]


def _build_overrides(
    max_loss_usd: float,
    emergency_mult: float,
    ug_fast: bool,
    ug_peak: bool,
    ug_soft: bool,
) -> Dict[str, Dict[str, Any]]:
    """Return profile_overrides dict for every mode/profile pair."""
    params = {
        "max_loss_usd_per_trade": max_loss_usd,
        "emergency_gap_loss_multiplier": emergency_mult,
        "ug_fast_loss_cut_enabled": ug_fast,
        "ug_peak_drawdown_cut_enabled": ug_peak,
        "ug_soft_supervisor_loss_escalation_enabled": ug_soft,
    }
    return {f"{mode}_{profile}": params for mode in MODES for profile in PROFILES}


CONFIGS = {
    "baseline": _build_overrides(
        max_loss_usd=9999.0,
        emergency_mult=9999.0,
        ug_fast=True,
        ug_peak=True,
        ug_soft=True,
    ),
    "guardrail_usd": _build_overrides(
        max_loss_usd=18.0,
        emergency_mult=1.5,
        ug_fast=False,
        ug_peak=False,
        ug_soft=False,
    ),
}


def load_window(window_id: str) -> Dict[str, Any]:
    path = WINDOWS_DIR / f"{window_id}.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _run_custom_loop(
    runner: SimulationRunner,
    components: Any,
    max_cycles: Optional[int] = None,
) -> Dict[str, Any]:
    """Mirror SimulationRunner.run but return components for post-analysis."""
    from datetime import timedelta

    results: List[Dict[str, Any]] = []
    transitions: List[Dict[str, Any]] = []
    cycle = 0
    current = runner.start
    next_cycle_minutes: Optional[int] = None
    last_successful_step: Optional[int] = None
    last_mode: Optional[str] = None

    while current <= runner.end:
        runner.clock._now = current
        runner._invalidate_caches()
        runner._inject_historical_screener()
        runner.news_client.update_for_date(current)
        try:
            result = runner.run_cycle(components=components)
            balance = runner.spot_client.get_account_balance()
            total = float(balance.get("USDT", 0.0))
            for asset, qty in balance.items():
                if asset == "USDT":
                    continue
                try:
                    price = runner.spot_client.get_symbol_price(f"{asset}USDT", use_cache=False)
                    total += float(qty) * price
                except Exception:
                    pass

            mode = str(result.get("regime_mode", "SPOT_BULL")).upper()
            if last_mode is not None and mode != last_mode:
                transitions.append({
                    "cycle": cycle,
                    "timestamp": runner.clock.now().isoformat(),
                    "from_mode": last_mode,
                    "to_mode": mode,
                    "policy": result.get("transition_policy", {}),
                })
            last_mode = mode

            results.append({
                "cycle": cycle,
                "timestamp": runner.clock.now().isoformat(),
                "result": result,
                "total_value": total,
                "usdt_balance": float(balance.get("USDT", 0.0)),
                "regime_mode": mode,
                "next_cycle_minutes": result.get("next_cycle_minutes"),
            })
            next_cycle_minutes = result.get("next_cycle_minutes")
            if next_cycle_minutes is not None:
                last_successful_step = next_cycle_minutes
        except Exception as e:
            print(f"[GuardrailSweep] Cycle {cycle} failed at {runner.clock.now().isoformat()}: {e}")
            traceback.print_exc()
            next_cycle_minutes = None

        cycle += 1
        step_minutes = next_cycle_minutes or last_successful_step
        if step_minutes is None:
            step_minutes = int(runner.interval.replace("h", "")) * 60 if runner.interval.endswith("h") else 60
        current = current + timedelta(minutes=step_minutes)
        if max_cycles is not None and cycle >= max_cycles:
            break

    return {
        "cycles": cycle,
        "start": runner.start.isoformat(),
        "end": runner.end.isoformat(),
        "final_balance": runner.spot_client.get_account_balance(),
        "results": results,
        "transitions": transitions,
    }


def _extract_closed_trades(pm: Any) -> List[Dict[str, Any]]:
    trades = []
    for pos in getattr(pm, "_closed_positions", []):
        trades.append({
            "symbol": getattr(pos, "symbol", None),
            "side": getattr(pos, "side", None),
            "quantity": float(getattr(pos, "quantity", 0) or 0),
            "entry_price": float(getattr(pos, "entry_price", 0) or 0),
            "exit_price": float(getattr(pos, "exit_price", 0) or 0),
            "pnl": float(getattr(pos, "pnl", 0) or 0),
            "pnl_percentage": float(getattr(pos, "pnl_percentage", 0) or 0),
            "close_reason": getattr(pos, "close_reason", None),
            "entry_time": getattr(pos, "entry_time", None),
            "exit_time": getattr(pos, "exit_time", None),
        })
    return trades


def _compute_metrics(trades: List[Dict[str, Any]], results: List[Dict[str, Any]]) -> Dict[str, Any]:
    losses = [t for t in trades if t["pnl"] < 0]
    wins = [t for t in trades if t["pnl"] > 0]
    total_pnl = sum(t["pnl"] for t in trades)
    win_rate = len(wins) / len(trades) if trades else 0.0
    max_loss = min((t["pnl"] for t in trades), default=0.0)
    losses_over_20 = [t for t in trades if t["pnl"] < -20]
    losses_over_25 = [t for t in trades if t["pnl"] < -25]
    emergency_like = [t for t in trades if t["pnl"] < -27]
    final_value = results[-1]["total_value"] if results else 0.0

    reason_counts: Dict[str, int] = {}
    for t in trades:
        reason_counts[t.get("close_reason") or "unknown"] = reason_counts.get(t.get("close_reason") or "unknown", 0) + 1

    return {
        "trade_count": len(trades),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(win_rate, 4),
        "total_pnl": round(total_pnl, 2),
        "final_value": round(final_value, 2),
        "max_loss": round(max_loss, 2),
        "losses_over_20_count": len(losses_over_20),
        "losses_over_20_pct": round(len(losses_over_20) / len(losses), 4) if losses else 0.0,
        "losses_over_25_count": len(losses_over_25),
        "emergency_gap_like_count": len(emergency_like),
        "close_reasons": reason_counts,
    }


def run_single_config(
    window: Dict[str, Any],
    label: str,
    profile_overrides: Dict[str, Dict[str, Any]],
    max_cycles: Optional[int] = None,
) -> Dict[str, Any]:
    settings = get_settings()
    data_dir = repo_root / "historical_data"
    symbols = [
        s for s in window["symbols"]
        if (data_dir / f"{s}_1h.parquet").exists()
    ]
    if not symbols:
        raise ValueError(f"No 1h parquet data available for any symbol in window {window['window_id']}")
    if "BTCUSDT" not in symbols:
        symbols = ["BTCUSDT", *symbols]
    start = datetime.fromisoformat(window["start"])
    end = datetime.fromisoformat(window["end"])

    runner = SimulationRunner(
        settings=settings,
        symbols=symbols,
        interval="1h",
        start=start,
        end=end,
        initial_usdt=2910.0,
        fear_greed_value=50.0,
        data_dir=str(repo_root / "historical_data"),
        forced_profile="B",
        profile_overrides=profile_overrides,
    )
    components = runner.build_components()
    run_data = _run_custom_loop(runner, components, max_cycles=max_cycles)
    trades = _extract_closed_trades(components.position_manager)
    metrics = _compute_metrics(trades, run_data["results"])

    return {
        "label": label,
        "window_id": window["window_id"],
        "metrics": metrics,
        "trades": trades,
        "cycles": run_data["cycles"],
        "start": run_data["start"],
        "end": run_data["end"],
        "final_balance": run_data["final_balance"],
        "transitions": run_data["transitions"],
    }


def run_sweep(
    window_ids: List[str],
    output_dir: Path,
    max_cycles: Optional[int] = None,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary: Dict[str, Any] = {"windows": [], "generated_at": datetime.now(timezone.utc).isoformat()}

    for window_id in window_ids:
        window = load_window(window_id)
        print(f"\n[GuardrailSweep] Window {window_id}: {window['start']} -> {window['end']}")
        window_result = {"window_id": window_id, "configs": []}

        for label, overrides in CONFIGS.items():
            print(f"  Running config '{label}' ...")
            try:
                result = run_single_config(window, label, overrides, max_cycles=max_cycles)
            except Exception as e:
                print(f"  FAILED config '{label}': {e}")
                traceback.print_exc()
                result = {"label": label, "error": str(e)}

            out_path = output_dir / f"{window_id}_{label}.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, default=str)
            print(f"  Saved: {out_path}")

            window_result["configs"].append({
                "label": label,
                "metrics": result.get("metrics", {}),
                "error": result.get("error"),
            })

        summary["windows"].append(window_result)

    summary_path = output_dir / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\n[GuardrailSweep] Summary saved: {summary_path}")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="MIMIC A/B sweep for USD loss guardrail")
    parser.add_argument("--windows", default=None, help="Comma-separated window IDs (default: all in Dev/benchmarks/windows)")
    parser.add_argument("--output-dir", required=True, help="Output directory for sweep results")
    parser.add_argument("--max-cycles", type=int, default=None, help="Limit cycles per simulation (for quick smoke tests)")
    args = parser.parse_args()

    if args.windows:
        window_ids = [w.strip() for w in args.windows.split(",") if w.strip()]
    else:
        window_ids = sorted(
            [p.stem for p in WINDOWS_DIR.glob("win_*.json")]
        )

    if not window_ids:
        print("[GuardrailSweep] No windows found.")
        return 1

    output_dir = Path(args.output_dir)
    run_sweep(window_ids, output_dir, max_cycles=args.max_cycles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import os
import random
from collections import Counter
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from lifecycle.base import Action, ExitDecision, ExitPolicy, MarketContext, MomentumExitRule, Position, Side, StagnationRule
from lifecycle.exit import ExitEngine
from super_mimic.certification import stable_hash
from super_mimic.core import DesignSpec, EngineResults


_POLICY_VARIANTS = (
    {"hard_sl_atr_mult": 1.5},
    {"hard_sl_atr_mult": 2.0},
    {"hard_sl_atr_mult": 3.0},
    {"hard_tp_atr_mult": 2.0},
    {"hard_tp_atr_mult": 4.0},
    {"trailing_activation_pct": 0.01, "trailing_atr_mult": 1.0},
    {"trailing_activation_pct": 0.02, "trailing_atr_mult": 2.0},
    {"breakeven_activation_pct": 0.01},
    {"stagnation_min_hours": 4.0},
    {"stagnation_min_hours": 12.0},
    {"time_stop_hours": 0.25, "time_stop_reason": "belief_exhausted"},
    {"time_stop_hours": 0.5, "time_stop_reason": "belief_exhausted"},
    {"time_stop_hours": 1.0, "time_stop_reason": "sr_path_stalled"},
    {"time_stop_hours": 1.5, "time_stop_reason": "sr_path_invalidated"},
    {"profit_erosion_pct": 0.015},
    {"profit_erosion_pct": 0.03},
    {"drawdown_stop_pct": 0.03},
    {"drawdown_stop_pct": 0.05},
    {"use_expected_gain_for_tp": True, "expected_gain_mult": 0.9},
    {"use_expected_gain_for_tp": True, "expected_gain_mult": 1.1},
)


def _base_policy(
    profile: str,
    overrides: Dict[str, float],
    regime: str = "RANGE_SCALP",
) -> ExitPolicy:
    return ExitPolicy(
        regime=regime,
        profile=profile,
        hard_sl_atr_mult=float(overrides.get("hard_sl_atr_mult", 2.0)),
        hard_tp_atr_mult=float(overrides.get("hard_tp_atr_mult", 3.0)),
        trailing_activation_pct=float(overrides.get("trailing_activation_pct", 0.015)),
        trailing_atr_mult=float(overrides.get("trailing_atr_mult", 1.5)),
        breakeven_activation_pct=float(overrides.get("breakeven_activation_pct", 0.01)),
        partial_takes=(),
        momentum_exit=MomentumExitRule(85, 15, 95, 5),
        stagnation_rule=StagnationRule(
            float(overrides.get("stagnation_min_hours", 6.0)),
            float(overrides.get("stagnation_max_drawdown_from_peak_pct", 0.05)),
            0.5,
        ),
        time_stop_hours=float(overrides.get("time_stop_hours", 0.0)) or None,
        use_expected_gain_for_tp=bool(overrides.get("use_expected_gain_for_tp", False)),
        expected_gain_mult=float(overrides.get("expected_gain_mult", 1.0)),
        allow_partial_exits=False,
    )


def _load_window(repo_root: str, manifest: Dict[str, Any]) -> Dict[str, pd.DataFrame]:
    data = {}
    start = pd.Timestamp(manifest["start"])
    end = pd.Timestamp(manifest["end"])
    for symbol in manifest.get("symbols") or []:
        fine_path = os.path.join(repo_root, "historical_data", f"{symbol}_5m.parquet")
        path = fine_path if os.path.exists(fine_path) else os.path.join(repo_root, "historical_data", f"{symbol}_1h.parquet")
        if not os.path.exists(path):
            continue
        frame = pd.read_parquet(path, columns=["open_time", "open", "high", "low", "close", "volume"])
        if pd.api.types.is_datetime64_any_dtype(frame["open_time"]):
            frame["timestamp"] = pd.to_datetime(frame["open_time"], utc=True, errors="coerce")
        else:
            numeric = pd.to_numeric(frame["open_time"], errors="coerce")
            unit = "ms" if numeric.dropna().abs().median() >= 100_000_000_000 else "s"
            frame["timestamp"] = pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")
        frame = frame[(frame["timestamp"] >= start) & (frame["timestamp"] <= end)].sort_values("timestamp")
        if len(frame) >= 25:
            data[symbol] = frame.reset_index(drop=True)
    return data


def _load_entry_cohort(path: Optional[str]) -> List[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return []
    cohort = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            for entry in ((row.get("e2e_evidence") or {}).get("lineage") or []):
                if entry.get("symbol") and entry.get("timestamp") and entry.get("entry_price"):
                    cohort.append({**entry, "window_id": row.get("window_id")})
    return cohort


def _replay_policy(
    data: Dict[str, pd.DataFrame],
    policy: ExitPolicy,
    cost_rate: float,
    cohort_entries: Optional[List[Dict[str, Any]]] = None,
    overrides: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    overrides = overrides or {}
    pnls: List[float] = []
    notionals: List[float] = []
    reasons: Counter = Counter()
    captures: List[float] = []
    cohort = []
    partials = 0
    censored = 0
    if cohort_entries:
        specs = []
        for item in cohort_entries:
            symbol = item.get("symbol")
            frame = data.get(symbol)
            if frame is None or frame.empty:
                continue
            timestamp = pd.Timestamp(item["timestamp"])
            eligible = frame.index[frame["timestamp"] >= timestamp].tolist()
            if not eligible or eligible[0] >= len(frame) - 1:
                continue
            specs.append((symbol, frame, eligible[0], item))
    else:
        specs = [
            (symbol, frame, min(24, len(frame) - 2), {})
            for symbol, frame in data.items()
        ]
    for index, (symbol, frame, entry_index, item) in enumerate(specs):
        entry = float(item.get("entry_price") or frame.iloc[entry_index]["close"])
        selected_contract = item.get("selected_contract") or {}
        raw_side = str(
            item.get("selected_side")
            or item.get("executed_side")
            or selected_contract.get("side")
            or ""
        ).upper()
        if "SHORT" in raw_side or raw_side == "SELL":
            side = Side.SHORT
        elif "LONG" in raw_side or raw_side == "BUY":
            side = Side.LONG
        elif cohort_entries:
            raise ValueError(f"Lifecycle cohort entry has no explicit side: {symbol}")
        else:
            side = Side.LONG if index % 2 == 0 else Side.SHORT
        quantity = float(item.get("quantity") or 1000.0 / entry)
        metadata: Dict[str, Any] = {}
        if policy.use_expected_gain_for_tp:
            metadata["expected_peak_return"] = 0.03
        position = Position(
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=entry,
            entry_time=float(frame.iloc[entry_index]["timestamp"].timestamp()),
            position_usdt=quantity * entry,
            category="CERTIFICATION",
            regime_at_entry=policy.regime,
            profile_at_entry=policy.profile,
            metadata=metadata,
        )
        cohort.append({
            "symbol": symbol,
            "side": side.value,
            "entry_price": entry,
            "entry_time": position.entry_time,
            "quantity": quantity,
            "selected_contract": item.get("selected_contract"),
        })
        future = frame.iloc[entry_index + 1:]
        best_pnl = 0.0
        exit_price = float(future.iloc[-1]["close"])
        exit_reason = "window_end"
        for _, bar in future.iterrows():
            price = float(bar["close"])
            atr = max(float(bar["high"]) - float(bar["low"]), entry * 0.001)
            pnl_pct = (price - entry) / entry if side == Side.LONG else (entry - price) / entry
            best_pnl = max(best_pnl, pnl_pct)
            position.peak_pnl_pct = best_pnl
            engine = ExitEngine(
                price_resolver=lambda _symbol, value=price: value,
                atr_resolver=lambda _symbol, value=atr: value,
                rsi_resolver=lambda _symbol: 50.0,
            )
            ctx = MarketContext(
                timestamp=float(bar["timestamp"].timestamp()),
                regime=policy.regime,
                regime_confidence={policy.regime: 1.0},
                btc_7d_pct=0.0,
                btc_30d_pct=0.0,
                btc_day_pct=0.0,
                fear_greed_index=50.0,
                btc_dominance=50.0,
            )
            decision = engine.evaluate([position], ctx, policy, ctx.timestamp)[0]

            # Manual certification variants for additional close forms not
            # produced by the isolated ExitEngine (e.g. profit erosion, drawdown)
            profit_erosion_pct = float(overrides.get("profit_erosion_pct", 0.0))
            drawdown_stop_pct = float(overrides.get("drawdown_stop_pct", 0.0))
            if decision.action == Action.HOLD:
                if profit_erosion_pct > 0 and best_pnl > 0 and (best_pnl - pnl_pct) >= profit_erosion_pct:
                    decision = ExitDecision.close_all(symbol, "profit_erosion", 80)
                elif drawdown_stop_pct > 0 and pnl_pct <= -drawdown_stop_pct:
                    decision = ExitDecision.close_all(symbol, "drawdown_stop", 81)

            if decision.action.value == "CLOSE_PCT":
                partials += 1
            if decision.action.value == "CLOSE_ALL":
                exit_price = price
                exit_reason = decision.reason
                if exit_reason == "time stop" and "time_stop_reason" in overrides:
                    exit_reason = str(overrides["time_stop_reason"])
                break
        if exit_reason == "window_end":
            censored += 1
            reasons[exit_reason] += 1
            continue
        gross = quantity * ((exit_price - entry) if side == Side.LONG else (entry - exit_price))
        net = gross - (entry + exit_price) * quantity * cost_rate
        pnls.append(net)
        notionals.append(quantity * entry)
        reasons[exit_reason] += 1
        if best_pnl > 0:
            captures.append(max(0.0, (net / max(quantity * entry, 1.0)) / best_pnl))
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    return {
        "trades": len(pnls),
        "pnl_usdt": sum(pnls),
        "pnl_pct": sum(pnls) / max(1.0, sum(notionals)) * 100.0,
        "win_rate": len(wins) / len(pnls) * 100.0 if pnls else 0.0,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "peak_capture_ratio": sum(captures) / len(captures) if captures else 0.0,
        "exit_reasons": dict(reasons),
        "partial_exits": partials,
        "right_censored": censored,
        "entry_cohort_hash": stable_hash(cohort),
    }


def run_lifecycle_certification(design: DesignSpec, repo_root: str) -> Tuple[EngineResults, Dict[str, Any]]:
    windows_dir = design.window_manifest_dir or os.path.join(repo_root, "Dev", "benchmarks", "windows")
    selected = set(design.windows or [])
    manifests = []
    for name in sorted(os.listdir(windows_dir)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(windows_dir, name), "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        if not isinstance(manifest, dict) or not all(manifest.get(key) for key in ("window_id", "start", "end")):
            continue
        if selected and manifest.get("window_id") not in selected and name[:-5] not in selected:
            continue
        manifests.append(manifest)
    # Use all selected windows; max_jobs will subsample the resulting grid.
    source_cohort = _load_entry_cohort(design.entry_cohort_path)
    if not source_cohort:
        raise ValueError("Lifecycle certification requires a non-empty real entry cohort")
    results = []
    for manifest in manifests:
        data = _load_window(repo_root, manifest)
        window_cohort = [entry for entry in source_cohort if entry.get("window_id") == manifest.get("window_id")]
        if not window_cohort:
            raise ValueError(
                f"Lifecycle certification window has no entry cohort: {manifest.get('window_id')}"
            )
        for entry in window_cohort:
            selected_contract = entry.get("selected_contract") or {}
            observed_regime = str(
                entry.get("regime_mode")
                or entry.get("regime")
                or selected_contract.get("regime")
                or ""
            ).upper()
            if observed_regime != design.mode:
                raise ValueError(
                    "Lifecycle cohort regime mismatch for "
                    f"{entry.get('symbol')}: requested={design.mode}, observed={observed_regime or 'MISSING'}"
                )
        for variant_index, overrides in enumerate(_POLICY_VARIANTS):
            policy = _base_policy(design.profile, overrides, regime=design.mode)
            for cost_rate in (0.0002, 0.0004, 0.0006, 0.0008, 0.0010):
                metrics = _replay_policy(data, policy, cost_rate, window_cohort, overrides)
                results.append({
                    "window_id": manifest.get("window_id"),
                    "requested_regime": design.mode,
                    "observed_regime": design.mode,
                    "profile": design.profile,
                    "params": {**overrides, "cost_rate": cost_rate},
                    "policy_variant": variant_index,
                    **metrics,
                })
    if len(results) > design.max_jobs:
        selected = sorted(random.Random(design.seed).sample(range(len(results)), design.max_jobs))
        results = [results[index] for index in selected]
    cohort_hashes = sorted({result["entry_cohort_hash"] for result in results})
    from super_mimic.certification import build_differentiation_report, build_invariant_report, build_reproducibility_report
    summary = {
        "total_runs": len(results),
        "entry_cohort_source": "entry_results",
        "entry_cohort_records": len(source_cohort),
        "requested_regime": design.mode,
        "observed_regimes": sorted({result["observed_regime"] for result in results}),
        "entry_cohort_hashes": cohort_hashes,
        "partial_exits": sum(result["partial_exits"] for result in results),
        "right_censored": sum(result["right_censored"] for result in results),
        "differentiation": build_differentiation_report(results),
        "reproducibility": build_reproducibility_report(results),
        "invariants": build_invariant_report(results, "lifecycle"),
    }
    output_dir = design.output_dir or os.path.join(repo_root, "Dev", "benchmarks", "super_mimic", "lifecycle")
    os.makedirs(output_dir, exist_ok=True)
    results_path = os.path.join(output_dir, "lifecycle_results.jsonl")
    with open(results_path, "w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    report_path = os.path.join(output_dir, "lifecycle_report.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    return EngineResults(
        engine="lifecycle_certification",
        summary=summary,
        results=results,
        report_path=report_path,
        results_path=results_path,
    ), {}

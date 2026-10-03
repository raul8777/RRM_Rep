from __future__ import annotations

import contextlib
import copy
import io
import json
import os
from typing import Any, Dict, List, Tuple

from config import get_settings
from opportunity_contract import build_executable_opportunity
from run_trades import _kelly_position_size
from super_mimic.certification import build_differentiation_report, build_invariant_report, build_reproducibility_report
from super_mimic.core import DesignSpec, EngineResults


_KELLY_VARIANTS = (
    {"kelly_base_usdt": 100.0, "kelly_dynamic_base_pct": 0.05, "kelly_max_usdt": 600.0, "kelly_max_pct_cap": 0.20, "kelly_max_pct_liquid": 0.25},
    {"kelly_base_usdt": 150.0, "kelly_dynamic_base_pct": 0.08, "kelly_max_usdt": 800.0, "kelly_max_pct_cap": 0.25, "kelly_max_pct_liquid": 0.30},
    {"kelly_base_usdt": 200.0, "kelly_dynamic_base_pct": 0.10, "kelly_max_usdt": 1000.0, "kelly_max_pct_cap": 0.30, "kelly_max_pct_liquid": 0.35},
    {"kelly_base_usdt": 250.0, "kelly_dynamic_base_pct": 0.12, "kelly_max_usdt": 1200.0, "kelly_max_pct_cap": 0.35, "kelly_max_pct_liquid": 0.40},
    {"kelly_base_usdt": 300.0, "kelly_dynamic_base_pct": 0.15, "kelly_max_usdt": 1500.0, "kelly_max_pct_cap": 0.40, "kelly_max_pct_liquid": 0.50},
    {"kelly_base_usdt": 400.0, "kelly_dynamic_base_pct": 0.18, "kelly_max_usdt": 1800.0, "kelly_max_pct_cap": 0.45, "kelly_max_pct_liquid": 0.55},
    {"kelly_base_usdt": 500.0, "kelly_dynamic_base_pct": 0.20, "kelly_max_usdt": 2000.0, "kelly_max_pct_cap": 0.50, "kelly_max_pct_liquid": 0.60},
    {"kelly_base_usdt": 600.0, "kelly_dynamic_base_pct": 0.22, "kelly_max_usdt": 2200.0, "kelly_max_pct_cap": 0.55, "kelly_max_pct_liquid": 0.70},
    {"kelly_base_usdt": 750.0, "kelly_dynamic_base_pct": 0.25, "kelly_max_usdt": 2500.0, "kelly_max_pct_cap": 0.60, "kelly_max_pct_liquid": 0.80},
    {"kelly_base_usdt": 1000.0, "kelly_dynamic_base_pct": 0.30, "kelly_max_usdt": 3000.0, "kelly_max_pct_cap": 0.70, "kelly_max_pct_liquid": 0.90},
)

_SIGNAL_COHORTS = (
    {"probability": 0.52, "strength": 0.45, "liquid": 2500.0, "sl_pct": 0.008, "tp_pct": 0.016},
    {"probability": 0.56, "strength": 0.58, "liquid": 3500.0, "sl_pct": 0.010, "tp_pct": 0.020},
    {"probability": 0.61, "strength": 0.72, "liquid": 5000.0, "sl_pct": 0.012, "tp_pct": 0.024},
    {"probability": 0.66, "strength": 0.82, "liquid": 7500.0, "sl_pct": 0.015, "tp_pct": 0.030},
    {"probability": 0.72, "strength": 0.90, "liquid": 10000.0, "sl_pct": 0.020, "tp_pct": 0.040},
)


def _settings_for_variant(variant: Dict[str, float], trade_type: str) -> Any:
    settings = copy.deepcopy(get_settings())
    settings.kelly_enabled = True
    for key, value in variant.items():
        setattr(settings, key, value)
    settings.kelly_min_usdt = 0.0
    settings.futures_kelly_min_usdt = 0.0
    settings.current_regime_mode = "RANGE_SCALP"
    return settings


def run_kelly_certification(design: DesignSpec, repo_root: str) -> Tuple[EngineResults, Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    max_loss_usd = 18.0
    entry = 100.0
    for variant_index, variant in enumerate(_KELLY_VARIANTS):
        for cohort_index, cohort in enumerate(_SIGNAL_COHORTS):
            for trade_type in ("spot", "futures"):
                for regime in ("RANGE_SCALP", "SPOT_BULL", "PRESERVATION", "FUTURES_SHORT"):
                    for profile_label in ("A", "B", "C"):
                        settings = _settings_for_variant(variant, trade_type)
                        prediction = {"success_probability": cohort["probability"]}
                        with contextlib.redirect_stdout(io.StringIO()):
                            requested_margin = _kelly_position_size(
                                settings,
                                cohort["strength"],
                                prediction,
                                cohort["liquid"],
                                cohort["sl_pct"],
                                cohort["tp_pct"],
                                db_client=None,
                                trade_type=trade_type,
                                regime=regime,
                                profile_label=profile_label,
                            )
                        stop = entry * (1.0 - cohort["sl_pct"])
                        target = entry * (1.0 + cohort["tp_pct"])
                        contract = build_executable_opportunity(
                            "LONG",
                            "LONG_SUPPORT_BOUNCE",
                            {
                                "position_size_usdt": requested_margin,
                                "success_probability": cohort["probability"],
                                "stop_price": stop,
                                "target_price": target,
                            },
                            {"current_price": entry},
                            leverage=2 if trade_type == "futures" else 1,
                            trade_type=trade_type,
                            max_loss_usd=max_loss_usd,
                            entry_fee_bps=4.0,
                            exit_fee_bps=4.0,
                            slippage_bps=5.2,
                        )
                        payload = contract.to_dict()
                        risk_within_cap = payload["economic_risk_usdt"] <= max_loss_usd + 1e-9
                        margin_not_above = payload["margin_usdt"] <= requested_margin + 1e-9
                        results.append({
                            "window_id": f"kelly_cohort_{cohort_index}",
                            "profile": design.profile,
                            "params": {**variant, "trade_type": trade_type, "cohort": cohort_index, "regime": regime, "profile_label": profile_label},
                            "variant": variant_index,
                            "trade_type": trade_type,
                            "regime": regime,
                            "profile_label": profile_label,
                            "probability": cohort["probability"],
                            "strength": cohort["strength"],
                            "liquid_usdt": cohort["liquid"],
                            "kelly_requested_margin_usdt": requested_margin,
                            "contract_margin_usdt": payload["margin_usdt"],
                            "contract_notional_usdt": payload["notional_usdt"],
                            "economic_risk_usdt": payload["economic_risk_usdt"],
                            "expected_net_pnl_usdt": payload["expected_net_pnl_usdt"],
                            "leverage": payload["leverage"],
                            "trades": 1 if payload["executable"] else 0,
                            "pnl_usdt": payload["expected_net_pnl_usdt"],
                            "pnl_pct": payload["expected_net_pnl_usdt"] / max(payload["margin_usdt"], 1.0) * 100.0,
                            "capital_deployment_ratio": payload["margin_usdt"] / cohort["liquid"],
                            "kelly_contract": {
                                "risk_within_cap": risk_within_cap,
                                "margin_not_above_kelly": margin_not_above,
                                "stop_unchanged": abs(payload["effective_stop_price"] - stop) < 1e-12,
                                "target_unchanged": abs(payload["effective_target_price"] - target) < 1e-12,
                                "valid_geometry": payload["effective_stop_price"] < entry < payload["effective_target_price"],
                            },
                        })
    results = results[:design.max_jobs]
    summary = {
        "total_runs": len(results),
        "experiment_stage": "kelly",
        "differentiation": build_differentiation_report(results),
        "reproducibility": build_reproducibility_report(results),
        "invariants": build_invariant_report(results, "kelly"),
        "executable_contracts": sum(result["trades"] for result in results),
        "scaled_contracts": sum(
            result["contract_margin_usdt"] + 1e-9 < result["kelly_requested_margin_usdt"]
            for result in results
        ),
    }
    output_dir = design.output_dir or os.path.join(repo_root, "Dev", "benchmarks", "super_mimic", "kelly")
    os.makedirs(output_dir, exist_ok=True)
    results_path = os.path.join(output_dir, "kelly_results.jsonl")
    with open(results_path, "w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    report_path = os.path.join(output_dir, "kelly_report.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    return EngineResults(
        engine="kelly_certification",
        summary=summary,
        results=results,
        report_path=report_path,
        results_path=results_path,
    ), {}

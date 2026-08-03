from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from range_scalp_data_contract import REQUIRED_FEATURES, SCHEMA_VERSION

EXCLUDED_REASONS = {
    "ghost_balance_mismatch",
    "dust_below_min_notional",
    "manual_close",
    "manual_urgent_cleanup_20260707",
    "manual_urgent_cleanup_remaining_20260707",
    "manual_force_close_dust_runner",
    "manual_force_close_small_runner",
    "manual_liquidation_idle_capital",
    "manual_rebalance",
}


def _dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _iso(value: Any) -> Optional[str]:
    parsed = _time(value)
    return parsed.isoformat() if parsed else None


def _meta_regime(meta: Dict[str, Any]) -> str:
    return str(meta.get("entry_regime") or meta.get("regime_at_entry") or meta.get("regime_mode") or meta.get("regime") or "UNKNOWN").upper()


def _trade_type(row: Dict[str, Any], meta: Dict[str, Any]) -> str:
    explicit = str(meta.get("trade_type") or "").lower()
    if explicit in ("spot", "futures"):
        return explicit
    return "futures" if str(row.get("side") or "").upper() in ("LONG", "SHORT") else "spot"


def _position_key(row: Dict[str, Any], meta: Dict[str, Any]) -> str:
    group = meta.get("close_group_id")
    if group:
        return f"group:{group}"
    order = meta.get("entry_order_id") or meta.get("order_id")
    if order:
        return f"order:{order}:{row.get('symbol')}:{row.get('side')}"
    return f"legacy:{row.get('symbol')}:{row.get('side')}:{_iso(row.get('entry_time')) or ''}"


def _feature_payload(meta: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = _dict(meta.get("entry_live_features"))
    return snapshot if snapshot else {}


def _profile(meta: Dict[str, Any], ready: Optional[Dict[str, Any]]) -> str:
    return str(meta.get("peak_quality_profile") or meta.get("entry_profile_label") or meta.get("profile_label") or (ready or {}).get("profile_label") or "UNKNOWN").upper()


def _outcome_summary(items: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(items)
    pnl = [_number(item["pnl"]) for item in rows]
    positive = sum(value for value in pnl if value > 0)
    negative = sum(value for value in pnl if value < 0)
    equity = peak = 0.0
    drawdown = 0.0
    for value in pnl:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    closed = len(rows)
    partial = sum(int(item["partial_count"] > 0) for item in rows)
    return {
        "mature_count": closed,
        "pnl_net_usdt": round(sum(pnl), 4),
        "pnl_net_pct": round(sum(_number(item["pnl_pct"]) for item in rows) / closed, 4) if closed else None,
        "profit_factor": round(positive / abs(negative), 4) if negative else None,
        "win_rate_pct": round(100.0 * sum(value > 0 for value in pnl) / closed, 2) if closed else 0.0,
        "max_drawdown_usdt": round(drawdown, 4),
        "partial_close_rate_pct": round(100.0 * partial / closed, 2) if closed else 0.0,
    }


def _gate(name: str, actual: Any, required: Any, passed: bool, reason: str) -> Dict[str, Any]:
    return {"name": name, "actual": actual, "required": required, "status": "passed" if passed else "blocked", "reason": reason}


def build_entry_evidence(
    db: Any,
    regime: str = "RANGE_SCALP",
    days: int = 30,
    profile: Optional[str] = None,
    trade_type: Optional[str] = None,
    include_legacy: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    regime = regime.upper()
    profile = profile.upper() if profile else None
    start = now.timestamp() - (days * 86400)
    ready_rows = db.execute_query(
        "SELECT id, status, regime_mode, profile_label, signal_meta, opened_at, opened_trade_id FROM ready_candidates",
    ) or []
    ready = {int(row["id"]): row for row in ready_rows if row.get("id") is not None}
    closed_rows = db.execute_query(
        "SELECT id, symbol, side, entry_time, exit_time, pnl, pnl_percentage, close_reason, metadata, is_synthetic, corrupt, is_corrupted FROM trades_closed WHERE exit_time >= NOW() - (%s * INTERVAL '1 day') AND COALESCE(is_corrupted, FALSE) = FALSE ORDER BY entry_time ASC, id ASC",
        (days,),
    ) or []
    candidate_rows = db.execute_query(
        "SELECT c.stage, c.decision, c.reason FROM entry_selection_candidates c "
        "JOIN entry_selection_cycles cycle ON cycle.cycle_id = c.cycle_id "
        "WHERE c.updated_at >= NOW() - (%s * INTERVAL '1 day') AND upper(cycle.regime_mode) = %s",
        (days, regime),
    ) or []

    ready_status = Counter()
    execution_blocks = Counter()
    snapshots: List[Dict[str, Any]] = []
    for row in ready_rows:
        signal_meta = _dict(row.get("signal_meta"))
        if str(row.get("regime_mode") or "").upper() != regime:
            continue
        if profile and _profile(signal_meta, row) != profile:
            continue
        snapshot = _feature_payload(signal_meta)
        if snapshot:
            snapshots.append({"snapshot": snapshot, "at": row.get("opened_at")})
        ready_status[str(row.get("status") or "unknown")] += 1
        execution_gate = _dict(_dict(row.get("data")).get("last_execution_gate"))
        if execution_gate.get("passed") is False:
            execution_blocks[str(execution_gate.get("reason") or "PREFLIGHT_BLOCKED")] += 1

    missing = Counter()
    for item in snapshots:
        missing.update(key for key in REQUIRED_FEATURES if item["snapshot"].get(key) is None)
    snapshot_count = len(snapshots)
    feature_total = snapshot_count * len(REQUIRED_FEATURES)
    completeness = (feature_total - sum(missing.values())) / feature_total if feature_total else 0.0
    latest_snapshot = max((_time(item["at"]) for item in snapshots if _time(item["at"])), default=None)

    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in closed_rows:
        meta = _dict(row.get("metadata"))
        if _meta_regime(meta) != regime or (trade_type and _trade_type(row, meta) != trade_type):
            continue
        groups[_position_key(row, meta)].append({**row, "_meta": meta})

    exact: List[Dict[str, Any]] = []
    eligible: List[Dict[str, Any]] = []
    lineage = Counter()
    excluded = Counter()
    close_reasons = Counter()
    profiles: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    trade_types: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    model_missing = Counter()
    scored = 0
    examples: List[Dict[str, Any]] = []

    for key, group in groups.items():
        first = group[0]
        meta = first["_meta"]
        raw_ready_id = meta.get("ready_candidate_id")
        try:
            ready_id = int(raw_ready_id) if raw_ready_id is not None else None
        except (TypeError, ValueError):
            ready_id = None
        candidate = ready.get(ready_id) if ready_id is not None else None
        status = "exact" if candidate else ("broken" if ready_id is not None else "legacy")
        lineage[status] += 1
        row_profile = _profile(meta, candidate)
        row_type = _trade_type(first, meta)
        reasons = [str(row.get("close_reason") or "unknown") for row in group]
        excluded_reason = next((reason for reason in reasons if reason in EXCLUDED_REASONS), None)
        if first.get("is_synthetic") or first.get("corrupt") or first.get("is_corrupted") or excluded_reason:
            excluded[excluded_reason or "invalid_source"] += 1
            continue
        item = {
            "key": key,
            "pnl": sum(_number(row.get("pnl")) for row in group),
            "pnl_pct": sum(_number(row.get("pnl_percentage")) for row in group) / len(group),
            "partial_count": sum(bool(_dict(row["_meta"]).get("partial_close")) for row in group),
            "profile": row_profile,
            "trade_type": row_type,
            "exit_time": max((_time(row.get("exit_time")) for row in group if _time(row.get("exit_time"))), default=None),
        }
        close_reasons.update(reasons)
        examples.append({"symbol": first.get("symbol"), "ready_candidate_id": ready_id, "link_status": status, "close_reason": reasons[-1], "exit_time": _iso(first.get("exit_time"))})
        if status == "exact":
            exact.append(item)
        if status == "exact" or include_legacy:
            if not profile or row_profile == profile:
                eligible.append(item)
                profiles[row_profile].append(item)
                trade_types[row_type].append(item)
        snapshot = _feature_payload(meta)
        if snapshot:
            model_missing.update(key for key in REQUIRED_FEATURES if snapshot.get(key) is None)
            scored += int(meta.get("peak_quality_score") is not None or _dict(meta.get("entry_profile_gate_info")).get("shadow_score") is not None)

    exact_a = [item for item in exact if item["profile"] == "A"]
    outcomes = _outcome_summary(eligible)
    exact_outcomes = _outcome_summary(exact)
    exact_rate = len(exact) / sum(lineage.values()) if lineage else 0.0
    coverage = len(exact) / max(1, len(groups))
    gates = [
        _gate("schema_completeness", round(completeness * 100, 2), 95.0, completeness >= 0.95, "Se requiere schema live completo en la cohorte shadow."),
        _gate("exact_lineage", round(exact_rate * 100, 2), 100.0, bool(exact), "Sólo los enlaces exactos cuentan para promoción."),
        _gate("tier_a_sample", len(exact_a), 30, len(exact_a) >= 30, "Se requieren 30 outcomes A maduros por holdout temporal."),
        _gate("net_pnl", exact_outcomes["pnl_net_usdt"], "> 0", exact_outcomes["pnl_net_usdt"] > 0 and bool(exact), "PnL neto agrupado de enlaces exactos."),
        _gate("profit_factor", exact_outcomes["profit_factor"], 1.15, (exact_outcomes["profit_factor"] or 0) >= 1.15, "PF de enlaces exactos maduros."),
        _gate("coverage", round(coverage * 100, 2), 5.0, coverage >= 0.05 and bool(exact), "Cobertura de outcomes exactos sobre cierres etiquetados."),
    ]
    alerts = []
    if not snapshot_count:
        alerts.append({"severity": "critical", "code": "SHADOW_SNAPSHOT_ABSENT", "message": "No hay snapshots range_scalp_live_v1 en la cohorte consultada.", "remediation": "Verificar que el contenedor desplegado capture y persista entry_live_features antes del gate."})
    elif completeness < 0.95:
        alerts.append({"severity": "warning", "code": "SHADOW_SCHEMA_INCOMPLETE", "message": f"Completeness de schema: {completeness:.1%}.", "remediation": "Completar producers de features sin imputar valores faltantes."})
    if not exact:
        alerts.append({"severity": "warning", "code": "EXACT_LINEAGE_INSUFFICIENT", "message": "No hay outcomes cerrados con enlace exacto en el filtro.", "remediation": "Verificar ready_candidate_id desde fill hasta el cierre agrupado."})
    if execution_blocks:
        reason, count = execution_blocks.most_common(1)[0]
        alerts.append({"severity": "warning", "code": "VALIDATED_PREFLIGHT_BLOCKED", "message": f"{count} candidata(s) staged siguen bloqueadas por {reason}.", "remediation": "Esperar un setup que vuelva a pasar el preflight; no relajar gates por este contador."})
    status = "ready_for_review" if all(gate["status"] == "passed" for gate in gates) else "collecting"
    if not snapshot_count:
        status = "blocked"

    funnel = Counter()
    for row in candidate_rows:
        stage = str(row.get("stage") or "unknown")
        funnel[stage] += 1
    return {
        "generated_at": now.isoformat(),
        "filters": {"regime": regime, "days": days, "profile": profile, "trade_type": trade_type, "include_legacy": include_legacy, "start_epoch": start},
        "instrumentation": {"feature_schema_version": SCHEMA_VERSION, "snapshots": snapshot_count, "snapshot_coverage_pct": round(completeness * 100, 2), "missing_by_feature": dict(sorted(missing.items())), "last_snapshot_at": latest_snapshot.isoformat() if latest_snapshot else None, "fresh": bool(latest_snapshot and (now - latest_snapshot).total_seconds() <= 7200)},
        "funnel": {"selection": len(candidate_rows), "qubo_selected": funnel.get("qubo_selected", 0), "staged": ready_status.get("ready", 0), "validated": ready_status.get("validated", 0), "validated_blocked_by_execution_gate": sum(execution_blocks.values()), "filled": ready_status.get("opened", 0), "closed": len(groups), "rejection_reasons": dict(funnel), "execution_gate_reasons": dict(execution_blocks)},
        "lineage": {"exact": len(exact), "legacy": lineage.get("legacy", 0), "broken": lineage.get("broken", 0), "excluded": dict(excluded), "exact_rate_pct": round(exact_rate * 100, 2), "examples": examples[:20]},
        "outcomes": {"exact_only": exact_outcomes, "selected": outcomes, "by_profile": {key: _outcome_summary(value) for key, value in sorted(profiles.items())}, "by_trade_type": {key: _outcome_summary(value) for key, value in sorted(trade_types.items())}, "by_exit_reason": dict(close_reasons.most_common()), "all_labeled_count": len(groups)},
        "shadow": {"model_mode": "shadow", "scored": scored, "missing_model_features": dict(sorted(model_missing.items())), "calibration_available": len(exact) >= 30},
        "promotion": {"overall_status": status, "gates": gates, "next_requirements": [gate["reason"] for gate in gates if gate["status"] != "passed"], "tier_a_deficit": max(0, 30 - len(exact_a))},
        "alerts": alerts,
    }


def build_sr_hunt_48h_report(
    db: Any,
    regime: str = "RANGE_SCALP",
    hours: int = 48,
) -> Dict[str, Any]:
    """Build a compact SR-hunt operational report for the last N hours.

    Focused on routing intent, side selection, execution outcomes and rejection
    causes to support short-priority governance and futures-long safety checks.
    """
    now = datetime.now(timezone.utc)
    regime_upper = str(regime or "RANGE_SCALP").strip().upper()
    window_hours = max(1, int(hours or 48))

    rc_rows = db.execute_query(
        """
        SELECT id, symbol, status, discard_reason, opened_at, discarded_at, signal_meta, data
        FROM ready_candidates
        WHERE upper(regime_mode) = %s
          AND (
                opened_at >= NOW() - (%s * INTERVAL '1 hour')
             OR discarded_at >= NOW() - (%s * INTERVAL '1 hour')
             OR status IN ('ready', 'validated')
          )
        ORDER BY id DESC
        """,
        (regime_upper, window_hours, window_hours),
    ) or []

    cand_rows = db.execute_query(
        """
        SELECT c.symbol, c.stage, c.decision, c.reason, c.data
        FROM entry_selection_candidates c
        JOIN entry_selection_cycles cycle ON cycle.cycle_id = c.cycle_id
        WHERE upper(cycle.regime_mode) = %s
          AND c.updated_at >= NOW() - (%s * INTERVAL '1 hour')
        """,
        (regime_upper, window_hours),
    ) or []

    opened_rows = db.execute_query(
        """
        SELECT symbol, side, entry_time, metadata FROM trades_open
        WHERE entry_time >= NOW() - (%s * INTERVAL '1 hour')
        """,
        (window_hours,),
    ) or []
    closed_rows = db.execute_query(
        """
        SELECT symbol, side, entry_time, metadata, pnl_usdt FROM trades_closed
        WHERE entry_time >= NOW() - (%s * INTERVAL '1 hour')
          AND COALESCE(is_corrupted, FALSE) = FALSE
        """,
        (window_hours,),
    ) or []

    by_routing_intent = Counter()
    by_selected_mode = Counter()
    by_trade_type_side = Counter()
    by_profile = Counter()
    by_category = Counter()
    ready_status = Counter()
    rc_rejection_reasons = Counter()
    execution_failures = Counter()

    for row in rc_rows:
        meta = _dict(row.get("signal_meta"))
        data = _dict(row.get("data"))
        arb = _dict(data.get("directional_arbitration"))
        routing_intent = str(meta.get("routing_intent") or "UNKNOWN").upper()
        selected_mode = str(
            arb.get("selected_mode")
            or meta.get("selected_mode")
            or meta.get("regime_mode")
            or "UNKNOWN"
        ).upper()
        trade_type = str(meta.get("trade_type") or ("futures" if "FUTURES" in selected_mode else "spot")).lower()
        expected_side = str(meta.get("expected_side") or ("SHORT" if "SHORT" in selected_mode else "LONG")).upper()
        profile = _profile(meta, row)
        category = str(meta.get("category") or "UNKNOWN").upper()
        status = str(row.get("status") or "unknown")

        by_routing_intent[routing_intent] += 1
        by_selected_mode[selected_mode] += 1
        by_trade_type_side[f"{trade_type}:{expected_side}"] += 1
        by_profile[profile] += 1
        by_category[category] += 1
        ready_status[status] += 1

        discard_reason = str(row.get("discard_reason") or "").strip()
        if discard_reason:
            rc_rejection_reasons[discard_reason] += 1
        gate = _dict(data.get("last_execution_gate"))
        if gate.get("passed") is False:
            execution_failures[str(gate.get("reason") or "PREFLIGHT_BLOCKED")] += 1

    funnel_stage = Counter()
    funnel_rejections = Counter()
    for row in cand_rows:
        stage = str(row.get("stage") or "unknown")
        decision = str(row.get("decision") or "pending")
        reason = str(row.get("reason") or "").strip()
        funnel_stage[stage] += 1
        if decision == "rejected" and reason:
            funnel_rejections[reason] += 1

    fill_by_trade_type = Counter()
    fill_confirmed_by_trade_type = Counter()
    side_calibration: Dict[str, Dict[str, Any]] = {}
    seen_entries = set()
    for row, is_closed in ([(r, False) for r in opened_rows] + [(r, True) for r in closed_rows]):
        meta = _dict(row.get("metadata"))
        symbol = str(row.get("symbol") or "")
        entry_time = _iso(row.get("entry_time")) or ""
        entry_order_id = str(meta.get("entry_order_id") or meta.get("order_id") or "")
        dedupe_key = (symbol, entry_time, entry_order_id)
        if dedupe_key in seen_entries:
            continue
        seen_entries.add(dedupe_key)
        trade_type = str(meta.get("trade_type") or "spot").lower()
        side = str(meta.get("expected_side") or row.get("side") or "LONG").upper()
        routing_intent = str(meta.get("routing_intent") or "UNKNOWN").upper()
        fill_by_trade_type[trade_type] += 1
        if bool(meta.get("binance_entry_confirmed", False)):
            fill_confirmed_by_trade_type[trade_type] += 1

        if is_closed:
            pnl_usdt = _number(row.get("pnl_usdt"))
            cohort_key = f"{trade_type}:{side}:{routing_intent}"
            slot = side_calibration.setdefault(
                cohort_key,
                {"trades": 0, "wins": 0, "losses": 0, "net_pnl_usdt": 0.0, "avg_pnl_usdt": 0.0},
            )
            slot["trades"] += 1
            slot["wins"] += int(pnl_usdt > 0.0)
            slot["losses"] += int(pnl_usdt < 0.0)
            slot["net_pnl_usdt"] += pnl_usdt

    fill_rates = {}
    for trade_type, total in fill_by_trade_type.items():
        confirmed = int(fill_confirmed_by_trade_type.get(trade_type, 0))
        fill_rates[trade_type] = {
            "entries": int(total),
            "fill_confirmed": confirmed,
            "fill_confirmed_rate_pct": round((confirmed / total) * 100.0, 2) if total else 0.0,
        }

    for key, slot in side_calibration.items():
        trades = max(1, int(slot.get("trades") or 0))
        wins = int(slot.get("wins") or 0)
        net_pnl = float(slot.get("net_pnl_usdt") or 0.0)
        slot["win_rate_pct"] = round((wins / trades) * 100.0, 2)
        slot["net_pnl_usdt"] = round(net_pnl, 6)
        slot["avg_pnl_usdt"] = round(net_pnl / trades, 6)

    return {
        "generated_at": now.isoformat(),
        "filters": {"regime": regime_upper, "hours": window_hours},
        "cohorts": {
            "routing_intent": dict(by_routing_intent),
            "selected_mode": dict(by_selected_mode),
            "trade_type_side": dict(by_trade_type_side),
            "profile": dict(by_profile),
            "category": dict(by_category),
        },
        "funnel": {
            "ready_status": dict(ready_status),
            "selection_stage": dict(funnel_stage),
            "selection_rejections": dict(funnel_rejections),
            "ready_candidate_rejections": dict(rc_rejection_reasons),
            "execution_failures": dict(execution_failures),
        },
        "execution": {
            "fill_confirmed": fill_rates,
            "entries_analyzed": int(sum(fill_by_trade_type.values())),
            "calibration_side_decisions": side_calibration,
        },
    }

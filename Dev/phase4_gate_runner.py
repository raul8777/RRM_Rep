#!/usr/bin/env python3
"""Phase 4 gate runner: critical drift-first + post-adjust acceptance metrics.

This script evaluates a single decision status for Phase 4 using the agreed gates:
- Gate 1: Drift entry gate (critical alerts + key parameter alignment)
- Gate 2: Traceability and restart-safe persistence gate
- Gate 3: Post-adjust performance gate (changepoint, SPOT/FUTURES split)
- Gate 4: Secondary structural parity check against backtest runs

Outputs a JSON report and prints a concise summary.
Exit codes:
- 0 PASS
- 2 FAIL_CRITICAL
- 3 FAIL_PERFORMANCE
- 4 INCONCLUSIVE
- 1 execution error
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_BASE_URL = os.environ.get("PHASE4_BASE_URL", "http://localhost:8000")
DEFAULT_API_KEY = os.environ.get(
    "PHASE4_API_KEY",
    "DfJsbMHzq1gYZo6rvTFk7RXU9ht3xLauS2yVQleBjCdPW4KI",
)
DEFAULT_RUN_IDS = [29600, 48412, 51631, 61556]
CRITICAL_PARAMS = [
    "futures_sl_pct",
    "futures_tp_pct",
    "kelly_max_pct_liquid",
    "risk_per_trade",
    "stop_loss_pct",
    "take_profit_pct",
]
REQUIRED_AUDIT_EVENTS = [
    "screener_selection",
    "profile_screener_bridge",
    "signal_decision",
    "order_open",
    "close_reason_normalized",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _pct_diff_rel(actual: float, expected: float) -> Optional[float]:
    if expected == 0:
        return 0.0 if actual == 0 else None
    return abs(actual - expected) / abs(expected)


class ApiClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def get(self, path: str, query: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        qs = ""
        if query:
            qs = "?" + urllib.parse.urlencode(query)
        url = f"{self.base_url}{path}{qs}"
        req = urllib.request.Request(url, headers={"X-API-Key": self.api_key})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))


class DbHandle:
    def __init__(self, db_url: str) -> None:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from db_client import DatabaseClient  # pylint: disable=import-outside-toplevel

        self.db = DatabaseClient(db_url)

    def q(self, sql: str, params: Optional[Tuple[Any, ...]] = None) -> List[Dict[str, Any]]:
        return self.db.execute_query(sql, params)


def _resolve_db_url(args_db_url: Optional[str]) -> str:
    if args_db_url:
        return args_db_url
    env = os.environ.get("DATABASE_URL")
    if env:
        return env
    return "postgresql://crypto_agent:crypto_agent@localhost:5432/crypto_agent"


def _gate1_drift_entry(api: ApiClient, tolerance_rel: float) -> Dict[str, Any]:
    alerts = api.get("/config/drift-alerts", {"acknowledged": "false", "limit": 500})
    drift = api.get("/config/drift")

    # Some legacy alert rows can break JSON parsing in the filtered endpoint.
    # Fallback to unfiltered payload and keep only unacknowledged rows.
    if not alerts.get("ok", True):
        fallback = api.get("/config/drift-alerts", {"limit": 2000})
        if fallback.get("ok"):
            alerts = {
                "ok": True,
                "alerts": [a for a in (fallback.get("alerts") or []) if not bool(a.get("acknowledged"))],
                "count": None,
            }

    open_alerts = alerts.get("alerts", []) or []
    critical_open_count = sum(1 for a in open_alerts if str(a.get("severity", "")).lower() == "critical")
    drift_rows = drift.get("drift", []) or []
    drift_count = len(drift_rows)

    expected_by_param: Dict[str, Any] = {}
    actual_by_param: Dict[str, Any] = {}
    for row in drift_rows:
        p = row.get("param")
        if not p:
            continue
        expected_by_param[p] = row.get("expected")
        actual_by_param[p] = row.get("actual")

    alignment_checks: List[Dict[str, Any]] = []
    all_critical_aligned = True
    for param in CRITICAL_PARAMS:
        expected = expected_by_param.get(param)
        actual = actual_by_param.get(param)
        exp_f = _to_float(expected)
        act_f = _to_float(actual)

        aligned = False
        rel_diff = None
        reason = "ok"

        if expected is None and actual is None:
            reason = "not_in_drift_assumed_aligned"
            aligned = True
        elif expected is None:
            reason = "expected_missing_in_drift"
            aligned = False
        elif exp_f is None or act_f is None:
            aligned = expected == actual
            reason = "non_numeric_compare" if aligned else "non_numeric_mismatch"
        else:
            rel_diff = _pct_diff_rel(act_f, exp_f)
            aligned = rel_diff is not None and rel_diff <= tolerance_rel
            reason = "within_tolerance" if aligned else "outside_tolerance"

        if not aligned:
            all_critical_aligned = False

        alignment_checks.append(
            {
                "param": param,
                "expected": expected,
                "actual": actual,
                "relative_diff": rel_diff,
                "aligned": aligned,
                "reason": reason,
            }
        )

    pass_critical = critical_open_count == 0
    pass_drift_count = drift_count <= 2
    passed = pass_critical and pass_drift_count and all_critical_aligned

    return {
        "gate": "gate_1_drift_entry",
        "passed": passed,
        "critical_open_count": critical_open_count,
        "drift_count": drift_count,
        "drift_count_threshold": 2,
        "critical_params_tolerance_rel": tolerance_rel,
        "critical_params_all_aligned": all_critical_aligned,
        "critical_param_checks": alignment_checks,
        "notes": [
            "Any remaining drift beyond threshold must be justified manually as no-impact-PnL.",
        ],
    }


def _event_aliases(allow_aliases: bool) -> Dict[str, List[str]]:
    if not allow_aliases:
        return {e: [e] for e in REQUIRED_AUDIT_EVENTS}
    return {
        "screener_selection": ["screener_selection"],
        "profile_screener_bridge": ["profile_screener_bridge"],
        "signal_decision": ["signal_decision"],
        "order_open": ["order_open", "open"],
        "close_reason_normalized": ["close_reason_normalized", "close"],
    }


def _gate2_traceability_persistence(
    db: DbHandle,
    allow_event_aliases: bool,
    sample_size: int,
) -> Dict[str, Any]:
    recent_closed = db.q(
        """
        SELECT id, symbol, entry_time, exit_time,
               COALESCE(NULLIF(metadata->>'trade_type',''), 'unknown') AS trade_type
        FROM trades_closed
        ORDER BY exit_time DESC
        LIMIT %s
        """,
        (sample_size,),
    )

    n_closed = len(recent_closed)
    type_valid = sum(1 for r in recent_closed if r.get("trade_type") in ("spot", "futures"))
    trade_type_ratio = (type_valid / n_closed) if n_closed else 0.0

    if n_closed:
        min_ts = recent_closed[-1]["entry_time"]
        max_ts = datetime.now(timezone.utc)
    else:
        min_ts = None
        max_ts = None

    counts_by_event: Dict[str, int] = {}
    aliases = _event_aliases(allow_event_aliases)
    required_ok = True

    if min_ts and max_ts:
        for logical_event, physical_events in aliases.items():
            placeholders = ",".join(["%s"] * len(physical_events))
            rows = db.q(
                f"""
                SELECT COUNT(*)::bigint AS n
                FROM trades_audit
                WHERE ts >= %s AND ts <= %s
                  AND event IN ({placeholders})
                """,
                tuple([min_ts, max_ts] + physical_events),
            )
            n = int(rows[0]["n"] if rows else 0)
            counts_by_event[logical_event] = n
            coverage = (n / n_closed) if n_closed else 0.0
            if coverage < 0.99:
                required_ok = False
    else:
        for logical_event in REQUIRED_AUDIT_EVENTS:
            counts_by_event[logical_event] = 0
        required_ok = False

    # Volatile-state presence check (restart-safe prerequisites)
    vrows = db.q(
        "SELECT value FROM trading_runtime_state WHERE key = 'volatile_state' LIMIT 1"
    )
    volatile = {}
    if vrows:
        volatile = vrows[0].get("value") or {}
        if isinstance(volatile, str):
            try:
                volatile = json.loads(volatile)
            except Exception:
                volatile = {}

    has_cb = volatile.get("circuit_breaker_until", None) is not None or "circuit_breaker_until" in volatile
    has_cf = "consecutive_failures" in volatile

    stale = db.q(
        """
        SELECT COUNT(*)::bigint AS n
        FROM market_data
        WHERE data_type = 'signal'
          AND COALESCE(data->>'processed', 'false') <> 'true'
          AND timestamp < (NOW() - INTERVAL '90 minutes')
        """
    )
    stale_unprocessed = int(stale[0]["n"] if stale else 0)

    passes_trade_type = trade_type_ratio == 1.0
    passes_audit = required_ok
    passes_state_presence = has_cb and has_cf
    passes_stale = stale_unprocessed == 0

    passed = passes_trade_type and passes_audit and passes_state_presence and passes_stale

    event_coverage = {}
    for evt, cnt in counts_by_event.items():
        event_coverage[evt] = {
            "count": cnt,
            "ratio_vs_recent_closed": (cnt / n_closed) if n_closed else 0.0,
            "threshold": 0.99,
        }

    return {
        "gate": "gate_2_traceability_persistence",
        "passed": passed,
        "recent_closed_sample": n_closed,
        "trade_type_valid_ratio": trade_type_ratio,
        "event_coverage": event_coverage,
        "volatile_state_has_circuit_breaker_until": has_cb,
        "volatile_state_has_consecutive_failures": has_cf,
        "stale_unprocessed_signals_over_90m": stale_unprocessed,
        "notes": [
            "Exact pre/post restart equality requires running capture-state before restart and verify-state after restart.",
            "Use --allow-event-aliases only if your runtime still logs open/close instead of order_open/close_reason_normalized.",
        ],
    }


def _scope(window: Dict[str, Any], key: str) -> Dict[str, Any]:
    return ((window.get("by_scope") or {}).get(key, {})) or {}


def _stop_loss_share_by_scope(db: DbHandle, start_ts: str, end_ts: str, scope: str) -> float:
    rows = db.q(
        """
        WITH base AS (
            SELECT
                CASE
                    WHEN COALESCE(NULLIF(metadata->>'trade_type',''), 'spot') = 'futures' THEN 'futures'
                    ELSE 'spot'
                END AS trade_scope,
                lower(COALESCE(close_reason, metadata->>'close_reason', 'unknown')) AS reason
            FROM trades_closed
            WHERE exit_time >= %s AND exit_time < %s
        )
        SELECT
            COUNT(*)::bigint AS total,
            COUNT(*) FILTER (WHERE reason LIKE 'stop_loss%%' OR reason = 'sl')::bigint AS stop_loss_n
        FROM base
        WHERE trade_scope = %s
        """,
        (start_ts, end_ts, scope),
    )
    if not rows:
        return 0.0
    total = int(rows[0].get("total") or 0)
    stop_n = int(rows[0].get("stop_loss_n") or 0)
    if total <= 0:
        return 0.0
    return stop_n / total


def _gate3_post_adjust_performance(api: ApiClient, db: DbHandle) -> Dict[str, Any]:
    perf = api.get("/metrics/performance-changepoint")
    if not perf.get("ok"):
        return {
            "gate": "gate_3_post_adjust_performance",
            "passed": False,
            "error": perf.get("error", "metrics endpoint failed"),
            "classification": "INCONCLUSIVE",
        }

    current_window = perf.get("current_window") or {}
    previous_window = perf.get("previous_window") or {}
    delta = perf.get("delta") or {}

    cur_spot = _scope(current_window, "spot")
    cur_fut = _scope(current_window, "futures")
    prev_spot = _scope(previous_window, "spot")
    prev_fut = _scope(previous_window, "futures")

    cur_spot_n = int(cur_spot.get("n_trades") or 0)
    cur_fut_n = int(cur_fut.get("n_trades") or 0)

    win_delta_spot = round(float(cur_spot.get("win_rate_pct") or 0.0) - float(prev_spot.get("win_rate_pct") or 0.0), 2)
    win_delta_fut = round(float(cur_fut.get("win_rate_pct") or 0.0) - float(prev_fut.get("win_rate_pct") or 0.0), 2)

    pnl_delta_spot = round(float(cur_spot.get("pnl_sum") or 0.0) - float(prev_spot.get("pnl_sum") or 0.0), 4)
    pnl_delta_fut = round(float(cur_fut.get("pnl_sum") or 0.0) - float(prev_fut.get("pnl_sum") or 0.0), 4)

    curr_start = str(current_window.get("start_ts"))
    curr_end = str(current_window.get("end_ts"))
    prev_start = str(previous_window.get("start_ts"))
    prev_end = str(previous_window.get("end_ts"))

    spot_sl_share_curr = _stop_loss_share_by_scope(db, curr_start, curr_end, "spot")
    spot_sl_share_prev = _stop_loss_share_by_scope(db, prev_start, prev_end, "spot")

    sl_reduction_rel = 0.0
    if spot_sl_share_prev > 0:
        sl_reduction_rel = (spot_sl_share_prev - spot_sl_share_curr) / spot_sl_share_prev

    cond_global = float(delta.get("net_pnl") or 0.0) > 0 and float(delta.get("win_rate_pct") or 0.0) >= 1.5
    cond_spot = pnl_delta_spot > 0 and sl_reduction_rel >= 0.15
    cond_fut = pnl_delta_fut >= 0 and win_delta_fut >= 0.0

    sample_ok = cur_spot_n >= 30 and cur_fut_n >= 15

    if not sample_ok:
        classification = "INCONCLUSIVE"
        passed = False
    elif cond_global and cond_spot and cond_fut:
        classification = "PASS"
        passed = True
    else:
        classification = "FAIL_PERFORMANCE"
        passed = False

    return {
        "gate": "gate_3_post_adjust_performance",
        "passed": passed,
        "classification": classification,
        "sample_ok": sample_ok,
        "sample_requirements": {"spot_min": 30, "futures_min": 15},
        "spot_current_n": cur_spot_n,
        "futures_current_n": cur_fut_n,
        "global_delta": {
            "net_pnl": float(delta.get("net_pnl") or 0.0),
            "win_rate_pct": float(delta.get("win_rate_pct") or 0.0),
        },
        "spot_delta": {
            "pnl_sum": pnl_delta_spot,
            "win_rate_pct": win_delta_spot,
            "stop_loss_share_current": round(spot_sl_share_curr, 6),
            "stop_loss_share_previous": round(spot_sl_share_prev, 6),
            "stop_loss_reduction_rel": round(sl_reduction_rel, 6),
        },
        "futures_delta": {
            "pnl_sum": pnl_delta_fut,
            "win_rate_pct": win_delta_fut,
        },
        "thresholds": {
            "global": {"net_pnl_delta_gt": 0, "win_rate_delta_gte_pp": 1.5},
            "spot": {"net_pnl_delta_gt": 0, "stop_loss_share_reduction_rel_gte": 0.15},
            "futures": {"net_pnl_delta_gte": 0, "win_rate_delta_gte_pp": 0.0},
        },
    }


def _capture_parity_baseline(api: ApiClient, run_ids: List[int], out_path: str) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for run_id in run_ids:
        data = api.get("/backtest/compare-vs-live", {"run_id": run_id})
        if not data.get("ok"):
            rows.append({"run_id": run_id, "ok": False, "error": data.get("error", "unknown")})
            continue
        bt_pnl = float((((data.get("backtest") or {}).get("summary") or {}).get("pnl_total") or 0.0))
        live_pnl = float((((data.get("live") or {}).get("summary") or {}).get("pnl_total") or 0.0))
        gap_abs = abs(bt_pnl - live_pnl)
        rows.append(
            {
                "run_id": run_id,
                "ok": True,
                "captured_at": _utc_now_iso(),
                "bt_pnl_total": bt_pnl,
                "live_pnl_total": live_pnl,
                "gap_abs": gap_abs,
            }
        )

    payload = {
        "captured_at": _utc_now_iso(),
        "run_ids": run_ids,
        "rows": rows,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)

    return payload


def _gate4_secondary_parity(
    api: ApiClient,
    run_ids: List[int],
    pre_gap_path: Optional[str],
) -> Dict[str, Any]:
    pre_map: Dict[int, float] = {}
    pre_loaded = False
    if pre_gap_path and os.path.exists(pre_gap_path):
        with open(pre_gap_path, "r", encoding="utf-8") as f:
            pre = json.load(f)
        for row in pre.get("rows", []):
            rid = int(row.get("run_id"))
            pre_map[rid] = float(row.get("gap_abs") or 0.0)
        pre_loaded = True

    rows: List[Dict[str, Any]] = []
    all_non_severe = True

    for run_id in run_ids:
        data = api.get("/backtest/compare-vs-live", {"run_id": run_id})
        if not data.get("ok"):
            rows.append({"run_id": run_id, "ok": False, "error": data.get("error", "unknown")})
            all_non_severe = False
            continue

        bt_pnl = float((((data.get("backtest") or {}).get("summary") or {}).get("pnl_total") or 0.0))
        live_pnl = float((((data.get("live") or {}).get("summary") or {}).get("pnl_total") or 0.0))
        current_gap = abs(bt_pnl - live_pnl)
        pre_gap = pre_map.get(run_id)

        improved_rel = None
        improved_20pct = None
        if pre_gap is not None and pre_gap > 0:
            improved_rel = (pre_gap - current_gap) / pre_gap
            improved_20pct = improved_rel >= 0.20
            if not improved_20pct:
                all_non_severe = False

        rows.append(
            {
                "run_id": run_id,
                "ok": True,
                "bt_pnl_total": bt_pnl,
                "live_pnl_total": live_pnl,
                "current_gap_abs": current_gap,
                "pre_gap_abs": pre_gap,
                "improved_rel": improved_rel,
                "improved_at_least_20pct": improved_20pct,
            }
        )

    classification = "INCONCLUSIVE"
    passed = False
    if not pre_loaded:
        classification = "INCONCLUSIVE"
        passed = False
    else:
        comparable = [r for r in rows if r.get("ok") and r.get("improved_rel") is not None]
        unchanged = [r for r in comparable if float(r.get("improved_rel") or 0.0) == 0.0]
        if comparable and len(unchanged) == len(comparable):
            classification = "INCONCLUSIVE"
            passed = False
        else:
            classification = "PASS" if all_non_severe else "DEGRADED"
            passed = all_non_severe

    return {
        "gate": "gate_4_secondary_parity",
        "passed": passed,
        "classification": classification,
        "pre_gap_baseline_loaded": pre_loaded,
        "pre_gap_path": pre_gap_path,
        "rows": rows,
        "notes": [
            "Gate 4 is secondary; it does not block tactical deploy if Gate 1-3 pass.",
            "When pre/post parity gap is unchanged for all comparable runs, classification is INCONCLUSIVE (no evolution).",
        ],
    }


def _final_decision(g1: Dict[str, Any], g2: Dict[str, Any], g3: Dict[str, Any], g4: Dict[str, Any]) -> Tuple[str, int]:
    if not g1.get("passed") or not g2.get("passed"):
        return ("FAIL_CRITICAL", 2)

    g3_class = str(g3.get("classification") or "")
    if g3_class == "INCONCLUSIVE":
        return ("INCONCLUSIVE", 4)

    if not g3.get("passed"):
        return ("FAIL_PERFORMANCE", 3)

    if g4.get("classification") == "DEGRADED":
        return ("PASS_WITH_PARITY_PENDING", 0)

    return ("PASS", 0)


def _cmd_capture_baseline(args: argparse.Namespace) -> int:
    api = ApiClient(args.base_url, args.api_key, args.timeout)
    payload = _capture_parity_baseline(api, args.run_ids, args.out)
    print(json.dumps({"ok": True, "out": args.out, "rows": payload.get("rows", [])}, indent=2, sort_keys=True))
    return 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    api = ApiClient(args.base_url, args.api_key, args.timeout)
    db = DbHandle(_resolve_db_url(args.db_url))

    g1 = _gate1_drift_entry(api, args.param_tolerance_rel)
    g2 = _gate2_traceability_persistence(db, args.allow_event_aliases, args.sample_size)
    g3 = _gate3_post_adjust_performance(api, db)
    g4 = _gate4_secondary_parity(api, args.run_ids, args.pre_gap_file)

    status, code = _final_decision(g1, g2, g3, g4)

    report = {
        "generated_at": _utc_now_iso(),
        "status": status,
        "gate_1": g1,
        "gate_2": g2,
        "gate_3": g3,
        "gate_4": g4,
    }

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, sort_keys=True)

    summary = {
        "status": status,
        "gate_1_passed": bool(g1.get("passed")),
        "gate_2_passed": bool(g2.get("passed")),
        "gate_3_classification": g3.get("classification"),
        "gate_4_classification": g4.get("classification"),
        "report_file": args.out,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))

    if args.print_full:
        print(json.dumps(report, indent=2, sort_keys=True))

    return code


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Phase 4 gate runner")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="API base URL")
    p.add_argument("--api-key", default=DEFAULT_API_KEY, help="API key")
    p.add_argument("--timeout", type=int, default=20, help="HTTP timeout in seconds")

    sp = p.add_subparsers(dest="command", required=True)

    cap = sp.add_parser("capture-baseline", help="Capture pre-adjust parity gap baseline (Gate 4 reference)")
    cap.add_argument("--run-ids", type=int, nargs="+", default=DEFAULT_RUN_IDS)
    cap.add_argument("--out", required=True, help="Output JSON path")
    cap.set_defaults(func=_cmd_capture_baseline)

    ev = sp.add_parser("evaluate", help="Evaluate Gate 1-4 and return PASS/FAIL/INCONCLUSIVE")
    ev.add_argument("--db-url", default=None, help="PostgreSQL URL (defaults to DATABASE_URL or localhost)")
    ev.add_argument("--run-ids", type=int, nargs="+", default=DEFAULT_RUN_IDS)
    ev.add_argument("--pre-gap-file", default=None, help="JSON generated by capture-baseline")
    ev.add_argument("--sample-size", type=int, default=100, help="Recent closed trades sample for Gate 2")
    ev.add_argument("--param-tolerance-rel", type=float, default=0.005, help="Relative tolerance for critical params (0.005 = 0.5%)")
    ev.add_argument("--allow-event-aliases", action="store_true", help="Allow open/close as aliases for order_open/close_reason_normalized")
    ev.add_argument("--out", default=None, help="Write full report JSON")
    ev.add_argument("--print-full", action="store_true", help="Print full report JSON")
    ev.set_defaults(func=_cmd_evaluate)

    return p


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except Exception as exc:  # pragma: no cover
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())

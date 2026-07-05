"""Capture dashboard parity snapshots for Binance/runtime/dashboard correlation.

Usage:
  python Dev/dashboard_parity_snapshot.py --base-url http://localhost:8000 --count 3 --interval-sec 20
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _get_json(url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else {}
            if isinstance(payload, dict):
                payload.setdefault("_http_status", int(getattr(resp, "status", 200)))
                return payload
            return {"_http_status": int(getattr(resp, "status", 200)), "_payload": payload}
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return {
            "_error": "http_error",
            "_http_status": int(exc.code),
            "_message": str(exc),
            "_body": body[:500],
        }
    except URLError as exc:
        return {
            "_error": "url_error",
            "_http_status": 0,
            "_message": str(exc),
        }
    except Exception as exc:
        return {
            "_error": "unexpected_error",
            "_http_status": 0,
            "_message": str(exc),
        }


def _read_dotenv_key() -> str:
    # Prefer local process env, then repo .env to support one-shot diagnostics.
    env_key = os.environ.get("DASHBOARD_API_KEY", "").strip()
    if env_key:
        return env_key
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[1] / ".env",
    ]
    for p in candidates:
        try:
            if not p.exists():
                continue
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                raw = line.strip()
                if not raw or raw.startswith("#") or "=" not in raw:
                    continue
                k, v = raw.split("=", 1)
                if k.strip() == "DASHBOARD_API_KEY":
                    return v.strip().strip('"').strip("'")
        except Exception:
            continue
    return ""


def _resolve_endpoint(base_url: str, headers: Dict[str, str], candidates: List[str]) -> Tuple[str, Dict[str, Any]]:
    last: Dict[str, Any] = {}
    for p in candidates:
        payload = _get_json(f"{base_url}{p}", headers)
        last = payload
        status = int(payload.get("_http_status") or 0)
        # Accept first endpoint that exists (non-404), even if it requires auth (401).
        if status and status != 404:
            return p, payload
    return candidates[0], last


def _resolve_api_key(base_url: str) -> str:
    key = _read_dotenv_key()
    if key:
        return key
    for path in ("/frontend/runtime-config", "/runtime-config"):
        payload = _get_json(f"{base_url}{path}", {"Content-Type": "application/json"})
        if int(payload.get("_http_status") or 0) != 200:
            continue
        maybe = str(payload.get("api_key") or "").strip()
        if maybe:
            return maybe
    return ""


def _build_index(positions: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for p in positions or []:
        sym = str(p.get("symbol") or "").upper()
        side = str(p.get("side") or "").upper()
        ttype = str(p.get("trade_type") or "").lower()
        if not sym:
            continue
        out[f"{sym}_{ttype}_{side}"] = {
            "symbol": sym,
            "trade_type": ttype,
            "side": side,
            "hours_held": float(p.get("hours_held") or 0.0),
            "unrealized_pnl": float(p.get("unrealized_pnl") or 0.0),
            "qty": float(p.get("quantity") or 0.0),
            "entry_price": float(p.get("entry_price") or 0.0),
        }
    return out


def _build_futures_index(positions: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for p in positions or []:
        sym = str(p.get("symbol") or "").upper()
        side = str(p.get("side") or "").upper()
        if not sym:
            continue
        out[f"{sym}_futures_{side}"] = {
            "symbol": sym,
            "trade_type": "futures",
            "side": side,
            "hours_held": float(p.get("hours_held") or 0.0),
            "unrealized_pnl": float(p.get("pnl") or p.get("unrealized_pnl") or 0.0),
            "qty": float(p.get("qty") or p.get("quantity") or 0.0),
            "entry_price": float(p.get("entry_price") or 0.0),
        }
    return out


def _compare_rows(dash: Dict[str, Dict[str, Any]], fut: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    keys = sorted(set(dash.keys()) | set(fut.keys()))
    rows: List[Dict[str, Any]] = []
    for k in keys:
        d = dash.get(k)
        f = fut.get(k)
        status = "ok"
        notes: List[str] = []
        if d is None:
            status = "missing_dashboard"
            notes.append("present_in_futures_endpoint_only")
        elif f is None and d.get("trade_type") == "futures":
            status = "missing_futures_endpoint"
            notes.append("present_in_dashboard_only")
        if d and f:
            if abs(float(d.get("qty", 0.0)) - float(f.get("qty", 0.0))) > 1e-8:
                status = "qty_mismatch"
                notes.append("qty")
            if abs(float(d.get("unrealized_pnl", 0.0)) - float(f.get("unrealized_pnl", 0.0))) > 0.05:
                if status == "ok":
                    status = "pnl_mismatch"
                notes.append("pnl")
            if abs(float(d.get("hours_held", 0.0)) - float(f.get("hours_held", 0.0))) > 0.2:
                if status == "ok":
                    status = "age_mismatch"
                notes.append("hours_held")
        rows.append({
            "key": k,
            "symbol": (d or f or {}).get("symbol"),
            "trade_type": (d or f or {}).get("trade_type"),
            "side": (d or f or {}).get("side"),
            "dashboard": d,
            "futures_endpoint": f,
            "status": status,
            "notes": notes,
        })
    return rows


def run(base_url: str, api_key: str, count: int, interval_sec: int) -> Dict[str, Any]:
    resolved_api_key = str(api_key or "").strip() or _resolve_api_key(base_url)
    headers = {"Content-Type": "application/json"}
    if resolved_api_key:
        headers["X-API-Key"] = resolved_api_key

    snapshots: List[Dict[str, Any]] = []
    for idx in range(count):
        ts = datetime.now(timezone.utc).isoformat()
        summary = _get_json(f"{base_url}/dashboard/summary", headers)
        runtime_path, runtime_state = _resolve_endpoint(
            base_url,
            headers,
            ["/system/runtime-state", "/runtime-state", "/system/runtime_state"],
        )
        futures_positions = _get_json(f"{base_url}/futures/positions", headers)
        truth_path, truth = _resolve_endpoint(
            base_url,
            headers,
            ["/metrics/binance-truth", "/metrics/binance_truth", "/binance-truth"],
        )

        open_positions = ((summary or {}).get("open_positions") or {}).get("positions") or []
        dash_idx = _build_index(open_positions) if isinstance(open_positions, list) else {}
        fut_positions = (futures_positions or {}).get("positions") or []
        fut_idx = _build_futures_index(fut_positions) if isinstance(fut_positions, list) else {}

        rows = _compare_rows(dash_idx, fut_idx)
        mismatches = [r for r in rows if r.get("status") != "ok"]

        snapshots.append({
            "captured_at": ts,
            "snapshot_meta": (summary or {}).get("snapshot_meta") or {},
            "resolved_endpoints": {
                "runtime_state": runtime_path,
                "binance_truth": truth_path,
            },
            "endpoint_status": {
                "dashboard_summary": (summary or {}).get("_http_status"),
                "runtime_state": (runtime_state or {}).get("_http_status"),
                "futures_positions": (futures_positions or {}).get("_http_status"),
                "binance_truth": (truth or {}).get("_http_status"),
            },
            "binance_truth": truth,
            "dashboard_open_positions": {
                "count": len(open_positions),
                "spot_count": ((summary or {}).get("open_positions") or {}).get("spot_count"),
                "futures_count": ((summary or {}).get("open_positions") or {}).get("futures_count"),
                "total_unrealized_pnl": ((summary or {}).get("open_positions") or {}).get("total_unrealized_pnl"),
            },
            "runtime_state": runtime_state,
            "parity_rows": rows,
            "mismatch_count": len(mismatches),
            "mismatches": mismatches,
        })

        if idx < count - 1:
            time.sleep(max(interval_sec, 1))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "api_key_resolved": bool(resolved_api_key),
        "count": count,
        "interval_sec": interval_sec,
        "snapshots": snapshots,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture dashboard parity snapshots")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--interval-sec", type=int, default=20)
    parser.add_argument("--output", default="Dev/dashboard_parity_snapshot.json")
    args = parser.parse_args()

    data = run(
        base_url=args.base_url.rstrip("/"),
        api_key=args.api_key,
        count=max(1, args.count),
        interval_sec=max(1, args.interval_sec),
    )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(out_path), "snapshots": len(data.get("snapshots") or [])}))


if __name__ == "__main__":
    main()

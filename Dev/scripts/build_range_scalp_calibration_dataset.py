from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Set, Tuple

import psycopg2

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = REPO_ROOT / "Dev" / "benchmarks" / "entry_profile_calibration" / "range_scalp_calibration_dataset.jsonl"
DEFAULT_MIMIC_INPUT = REPO_ROOT / "Dev" / "benchmarks" / "entry_profile_calibration" / "training_data_v2_bestexit.jsonl"
DEFAULT_REAL_OUTCOMES = REPO_ROOT / "Dev" / "benchmarks" / "research_data" / "executed_position_outcomes.jsonl"
EXCLUDED_REASONS = {"ghost_balance_mismatch", "dust_below_min_notional", "manual_close"}


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _regime(metadata: Dict[str, Any]) -> str:
    return str(metadata.get("regime_at_entry") or metadata.get("regime_mode") or metadata.get("regime") or "").upper()


def _identity(row: Dict[str, Any]) -> Tuple[str, str, str]:
    return (
        str(row.get("source") or ""),
        str(row.get("symbol") or ""),
        str(row.get("entry_time") or row.get("timestamp") or ""),
    )


def _flatten_real_trade(row: Dict[str, Any]) -> Dict[str, Any] | None:
    metadata = _as_dict(row.get("metadata"))
    prediction = _as_dict(row.get("pnl_prediction"))
    indicators = _as_dict(row.get("indicators_at_entry"))
    signal = _as_dict(metadata.get("entry_signal") or metadata.get("signal_meta"))
    model_features = _as_dict(metadata.get("entry_model_features"))
    merged = {**indicators, **signal, **prediction, **metadata, **model_features}
    if _regime(merged) != "RANGE_SCALP":
        return None
    if str(row.get("close_reason") or "") in EXCLUDED_REASONS:
        return None
    if bool(row.get("is_synthetic")) or bool(row.get("corrupt")):
        return None
    pnl_pct = row.get("pnl_percentage")
    try:
        pnl_pct = float(pnl_pct)
    except (TypeError, ValueError):
        return None
    entry_time = row.get("entry_time")
    if entry_time is None:
        return None
    merged.update({
        "source": "real_closed_trade",
        "symbol": row.get("symbol"),
        "entry_time": entry_time.isoformat() if hasattr(entry_time, "isoformat") else str(entry_time),
        "exit_time": row.get("exit_time").isoformat() if hasattr(row.get("exit_time"), "isoformat") else str(row.get("exit_time") or ""),
        "regime": "RANGE_SCALP",
        "side": row.get("side"),
        "realized_net_pnl_pct": pnl_pct,
        "realized_net_pnl_usdt": float(row.get("pnl") or 0.0),
        "close_reason": row.get("close_reason"),
        "entry_profile": metadata.get("peak_quality_profile") or metadata.get("entry_profile_label"),
        "dataset_version": "range_scalp_v1",
    })
    return merged


def _iter_real_trades(days: int) -> Iterator[Dict[str, Any]]:
    db_url = os.environ.get("DB_URL", "postgresql://crypto_agent:crypto_agent_password@postgres:5432/crypto_agent")
    conn = psycopg2.connect(db_url)
    try:
        cursor = conn.cursor(name="range_scalp_calibration_trades")
        cursor.itersize = 500
        cursor.execute(
            """
            SELECT symbol, side, entry_time, exit_time, pnl, pnl_percentage, close_reason,
                   metadata, pnl_prediction, indicators_at_entry, is_synthetic, corrupt
            FROM trades_closed
            WHERE exit_time >= NOW() - (%s * INTERVAL '1 day')
            ORDER BY entry_time ASC
            """,
            (days,),
        )
        columns = (
            "symbol", "side", "entry_time", "exit_time", "pnl", "pnl_percentage", "close_reason",
            "metadata", "pnl_prediction", "indicators_at_entry", "is_synthetic", "corrupt",
        )
        for values in cursor:
            row = dict(zip(columns, values))
            flattened = _flatten_real_trade(row)
            if flattened:
                yield flattened
    finally:
        conn.close()


def _iter_research_outcomes(path: Path) -> Iterator[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("source") or "") != "real_closed_trade":
                continue
            if str(row.get("regime") or "").upper() != "RANGE_SCALP":
                continue
            if not row.get("training_eligible") or row.get("realized_net_pnl_pct") is None:
                continue
            features = _as_dict(row.get("entry_features"))
            yield {
                **features,
                "source": "real_closed_trade",
                "dataset_version": "range_scalp_v2",
                "position_key": row.get("position_key"),
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "entry_time": row.get("entry_time"),
                "exit_time": row.get("exit_time"),
                "regime": "RANGE_SCALP",
                "entry_profile": row.get("entry_quality_profile"),
                "close_reason_set": row.get("exit_reason_set", []),
                "realized_net_pnl_pct": row.get("realized_net_pnl_pct"),
                "realized_net_pnl_usdt": row.get("realized_net_pnl_usdt"),
                "feature_schema_version": row.get("feature_schema_version"),
                "feature_missing": row.get("feature_missing", []),
                "feature_completeness": row.get("feature_completeness"),
                "link_status": _as_dict(row.get("ready_candidate")).get("link_status"),
            }


def _iter_mimic_rows(path: Path) -> Iterator[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("regime") or "").upper() != "RANGE_SCALP":
                continue
            if row.get("realized_net_pnl_pct") is None:
                continue
            row["source"] = "mimic"
            row["dataset_version"] = "range_scalp_v2"
            row.setdefault("entry_time", row.get("run_id"))
            yield row


def _write_rows(output: Path, rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    seen: Set[Tuple[str, str, str]] = set()
    sources: Dict[str, int] = {}
    symbols: Set[str] = set()
    count = 0
    with output.open("w", encoding="utf-8") as handle:
        for row in rows:
            key = _identity(row)
            if key in seen:
                continue
            seen.add(key)
            row["feature_missing"] = sorted(
                key for key in ("rsi_4h", "vol_spike", "range_position", "atr", "success_probability")
                if row.get(key) is None
            )
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            count += 1
            source = str(row.get("source") or "unknown")
            sources[source] = sources.get(source, 0) + 1
            if row.get("symbol"):
                symbols.add(str(row["symbol"]))
    return {
        "dataset_version": "range_scalp_v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows": count,
        "sources": sources,
        "symbols": len(symbols),
        "output": str(output),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--mimic-input", type=Path, default=DEFAULT_MIMIC_INPUT)
    parser.add_argument("--real-outcomes", type=Path, default=DEFAULT_REAL_OUTCOMES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    def rows() -> Iterator[Dict[str, Any]]:
        yield from _iter_mimic_rows(args.mimic_input)
        if args.real_outcomes.exists():
            yield from _iter_research_outcomes(args.real_outcomes)
        else:
            yield from _iter_real_trades(args.days)

    print(json.dumps(_write_rows(args.output, rows()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

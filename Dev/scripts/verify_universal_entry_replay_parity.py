"""Differential-test replay outcomes against SimulationFuturesClient on archived candles."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from simulation.entry_outcome_resolver import resolve_bar_exit, resolve_contract_path
from simulation.mimic_clients import SimulationClock, SimulationFuturesClient, SimulationLeveragedPosition
from simulation.mimic_feed import HistoricalDataFeed


DEFAULT_DATA_DIR = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "normalized" / "pilot_development" / "spot"
DEFAULT_OUTPUT = REPO_ROOT / "Dev" / "benchmarks" / "universal_entry_selection" / "pilot_development" / "replay_parity_report.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(resolved)


def _mimic_one_bar(symbol: str, bar: Dict[str, Any], side: str, stop: float, target: float) -> Dict[str, Any]:
    timestamp = float(bar["open_time"])
    now = datetime.fromtimestamp(timestamp, timezone.utc)
    client = SimulationFuturesClient(
        SimulationClock(now),
        {symbol: [bar]},
        commission_rate=0.0,
        data_dir="",
    )
    position = SimulationLeveragedPosition(
        symbol=symbol,
        side=side,
        qty=1.0,
        entry_price=float(bar["open"]),
        sl_price=stop,
        tp_price=target,
        order_id=f"PARITY_{uuid.uuid4().hex[:12]}",
        margin_usdt=100.0,
        opened_at=timestamp,
    )
    client._available_usdt = 1000.0
    client._open_positions[f"{symbol}_{side}"] = position
    closes = client.check_positions(settings=None)
    if not closes:
        return {"outcome": "NONE", "exit_price": None, "ambiguous_intrabar": False, "gap_through": False}
    close = closes[0]
    return {
        "outcome": close["reason"],
        "exit_price": close["exit_price"],
        "ambiguous_intrabar": close.get("ambiguous_intrabar", False),
        "gap_through": close.get("gap_through", False),
    }


def verify_parity(data_dir: Path, symbols: List[str], sample_per_symbol: int) -> Dict[str, Any]:
    cases = []
    mismatches = []
    feed = HistoricalDataFeed(str(data_dir))
    for symbol in symbols:
        bars = feed.get_klines(symbol, "1m")
        selected = bars[:: max(1, len(bars) // max(1, sample_per_symbol))][:sample_per_symbol]
        for bar in selected:
            entry = float(bar["open"])
            if entry <= 0:
                continue
            for side in ("LONG", "SHORT"):
                stop = entry * (0.995 if side == "LONG" else 1.005)
                target = entry * (1.01 if side == "LONG" else 0.99)
                resolver_exit = resolve_bar_exit(side, bar, stop, target)
                resolver = {
                    "outcome": resolver_exit.reason if resolver_exit else "NONE",
                    "exit_price": resolver_exit.exit_price if resolver_exit else None,
                    "ambiguous_intrabar": resolver_exit.ambiguous_intrabar if resolver_exit else False,
                    "gap_through": resolver_exit.gap_through if resolver_exit else False,
                }
                mimic = _mimic_one_bar(symbol, bar, side, stop, target)
                cases.append({"symbol": symbol, "timestamp": bar["open_time"], "side": side})
                if resolver != mimic:
                    mismatches.append({"case": cases[-1], "resolver": resolver, "mimic": mimic})
    return {
        "report_version": "universal_entry_replay_parity_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": _display_path(data_dir),
        "symbols": symbols,
        "sample_per_symbol": sample_per_symbol,
        "cases": len(cases),
        "mismatches": len(mismatches),
        "parity_passed": not mismatches and bool(cases),
        "mismatch_examples": mismatches[:20],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT,BNBUSDT")
    parser.add_argument("--sample-per-symbol", type=int, default=20)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.sample_per_symbol <= 0:
        parser.error("--sample-per-symbol must be positive")
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    try:
        report = verify_parity(args.data_dir, symbols, args.sample_per_symbol)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    report["output"] = str(args.output.relative_to(REPO_ROOT)).replace("\\", "/")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
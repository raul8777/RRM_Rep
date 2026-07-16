"""peak_mimic_validator.py — simulador directo sobre peaks_all.parquet.

Abre posiciones en peak_start_ts, cierra en peak_end_ts, usando el perfil
A/B/C asignado por el modelo peak_quality. Reporta PnL, win-rate, drawdown,
profit factor y métricas por perfil.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import peak_signal_source
from peak_quality import PROFILE_OVERRIDES

logger = logging.getLogger(__name__)


def _position_size(profile: str, cash: float) -> float:
    """Tamaño de posición según perfil A/B/C y capital disponible."""
    overrides = PROFILE_OVERRIDES.get(profile, PROFILE_OVERRIDES["C"])
    max_usdt = float(overrides.get("max_usdt", 600.0))
    cap_pct = float(overrides.get("cap_pct", 0.25))
    return min(max_usdt, cash * cap_pct)


def _simulate(
    events: List[Dict[str, Any]],
    initial_capital: float = 4900.0,
    slippage_pct: float = 0.05,
    fee_pct: float = 0.1,
    min_trade: float = 10.0,
) -> Dict[str, Any]:
    """Simula todos los eventos globalmente, en orden cronológico.

    Cada evento es una apertura en peak_start_dt y un cierre en peak_end_dt.
    Se cierran primero las posiciones cuyo peak_end_dt <= timestamp actual y
    luego se intenta abrir la nueva posición con el capital libre.
    """
    # Crear una cola de eventos: (timestamp, tipo, objeto)
    # tipo: 'open' o 'close'. Cerrar primero para liberar capital.
    queue: List[Tuple[datetime, str, Dict[str, Any]]] = []
    for e in events:
        queue.append((e["peak_start_dt"], "open", e))
        queue.append((e["peak_end_dt"], "close", e))

    queue.sort(key=lambda x: (x[0], 0 if x[1] == "close" else 1))

    cash = float(initial_capital)
    equity_peak = cash
    max_drawdown_pct = 0.0
    open_positions: List[Dict[str, Any]] = []

    trades: List[Dict[str, Any]] = []
    profile_counts = {"A": 0, "B": 0, "C": 0}
    profile_wins = {"A": 0, "B": 0, "C": 0}
    profile_pnl = {"A": 0.0, "B": 0.0, "C": 0.0}
    wins = 0
    losses = 0
    total_pnl = 0.0
    gross_profit = 0.0
    gross_loss = 0.0

    for ts, kind, event in queue:
        if kind == "close":
            # Encontrar posición abierta correspondiente
            pos = None
            for i, p in enumerate(open_positions):
                if p["symbol"] == event["symbol"] and p["profile"] == event["peak_quality_profile"] and p["start"] == event["peak_start_dt"]:
                    pos = p
                    del open_positions[i]
                    break
            if pos is None:
                continue
            entry = pos["entry_price"]
            exit_ = pos["exit_price"]
            direction = pos["direction"]
            size = pos["size"]
            qty = size / entry
            gross = qty * (exit_ - entry)
            if direction == "SHORT":
                gross = -gross
            # fees (entrada + salida) sobre el nocional
            fee = 2 * size * (fee_pct / 100.0)
            slippage = size * (slippage_pct / 100.0)
            pnl = gross - fee - slippage
            cash += size + pnl
            total_pnl += pnl
            profile = pos["profile"]
            profile_pnl[profile] += pnl
            profile_counts[profile] += 1
            if pnl > 0:
                wins += 1
                profile_wins[profile] += 1
                gross_profit += pnl
            else:
                losses += 1
                gross_loss += abs(pnl)
            trades.append({
                "symbol": pos["symbol"],
                "profile": profile,
                "direction": direction,
                "size": round(size, 2),
                "entry": round(entry, 8),
                "exit": round(exit_, 8),
                "peak_return": pos["peak_return"],
                "pnl": round(pnl, 4),
                "start": pos["start"].isoformat(),
                "end": pos["end"].isoformat(),
            })
        else:
            # intentar abrir
            profile = event["peak_quality_profile"]
            direction = event.get("direction", "LONG")
            if direction not in ("LONG", "SHORT"):
                continue
            size = _position_size(profile, cash)
            if size < min_trade:
                continue
            cash -= size
            open_positions.append({
                "symbol": event["symbol"],
                "profile": profile,
                "direction": direction,
                "entry_price": float(event["entry_price"]),
                "exit_price": float(event["exit_price"]),
                "peak_return": float(event["peak_return"]),
                "size": size,
                "start": event["peak_start_dt"],
                "end": event["peak_end_dt"],
            })

        # Equity snapshot y drawdown (solo capital realizado + inversión en curso)
        invested = sum(p["size"] for p in open_positions)
        equity = cash + invested
        if equity > equity_peak:
            equity_peak = equity
        dd = (equity_peak - equity) / equity_peak if equity_peak > 0 else 0.0
        if dd > max_drawdown_pct:
            max_drawdown_pct = dd

    # Cerrar posiciones que queden abiertas al final usando exit_price
    for pos in open_positions:
        entry = pos["entry_price"]
        exit_ = pos["exit_price"]
        direction = pos["direction"]
        size = pos["size"]
        qty = size / entry
        gross = qty * (exit_ - entry)
        if direction == "SHORT":
            gross = -gross
        fee = 2 * size * (fee_pct / 100.0)
        slippage = size * (slippage_pct / 100.0)
        pnl = gross - fee - slippage
        cash += size + pnl
        total_pnl += pnl
        profile = pos["profile"]
        profile_pnl[profile] += pnl
        profile_counts[profile] += 1
        if pnl > 0:
            wins += 1
            profile_wins[profile] += 1
            gross_profit += pnl
        else:
            losses += 1
            gross_loss += abs(pnl)

    total_trades = wins + losses
    win_rate = wins / total_trades if total_trades > 0 else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    final_value = cash
    pnl_pct = (final_value - initial_capital) / initial_capital * 100.0

    return {
        "initial_capital": initial_capital,
        "final_value": round(final_value, 2),
        "pnl_usdt": round(total_pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate * 100.0, 2),
        "max_drawdown_pct": round(max_drawdown_pct * 100.0, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profile_counts": {k: v for k, v in profile_counts.items()},
        "profile_wins": {k: v for k, v in profile_wins.items()},
        "profile_pnl": {k: round(v, 2) for k, v in profile_pnl.items()},
        "trades": trades,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Peak MIMIC validator")
    parser.add_argument(
        "--parquet",
        type=str,
        default=str(peak_signal_source.DEFAULT_PARQUET),
        help="Ruta a peaks_all.parquet",
    )
    parser.add_argument(
        "--model-dir",
        type=str,
        default=str(peak_signal_source.DEFAULT_MODEL_DIR),
        help="Directorio con modelos peak_quality",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=4900.0,
        help="Capital inicial",
    )
    parser.add_argument(
        "--slippage-pct",
        type=float,
        default=0.05,
        help="Slippage por trade (%)",
    )
    parser.add_argument(
        "--fee-pct",
        type=float,
        default=0.1,
        help="Comisión total entrada+salida (%)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=200_000,
        help="Tamaño de lote para lectura parquet",
    )
    parser.add_argument(
        "--n-jobs",
        type=int,
        default=12,
        help="Workers para RandomForestRegressor.predict",
    )
    parser.add_argument(
        "--profiles",
        type=str,
        default="A,B,C",
        help="Perfiles a simular",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="peak_mimic_report.json",
        help="Ruta del reporte JSON",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    profiles = tuple(p.strip().upper() for p in args.profiles.split(",") if p.strip())

    logger.info("Construyendo schedule de señales peak...")
    schedule = peak_signal_source.build_peak_signal_schedule(
        parquet_path=args.parquet,
        model_dir=args.model_dir,
        batch_size=args.batch_size,
        n_jobs=args.n_jobs,
        include_profiles=profiles,
    )

    events: List[Dict[str, Any]] = []
    for window_id, evs in schedule.items():
        events.extend(evs)
    events.sort(key=lambda x: x["peak_start_dt"])
    logger.info("Total eventos a simular: %d", len(events))

    logger.info("Simulando %d eventos...", len(events))
    result = _simulate(
        events,
        initial_capital=args.capital,
        slippage_pct=args.slippage_pct,
        fee_pct=args.fee_pct,
    )

    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["parquet"] = args.parquet
    result["model_dir"] = args.model_dir

    Path(args.output).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    logger.info("Reporte guardado en %s", args.output)
    print(json.dumps({k: v for k, v in result.items() if k != "trades"}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

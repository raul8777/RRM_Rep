"""
benchmark_compare.py — Compara el estado actual del sistema contra el baseline guardado.

Uso (dentro del container):
    python Dev/scripts/benchmark_compare.py
    python Dev/scripts/benchmark_compare.py --baseline Dev/benchmarks/snapshot_20260620_2213.json

Uso desde Windows:
    docker exec crypto-agent-app python /app/Dev/scripts/benchmark_compare.py
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/app')

OUT_DIR = '/app/Dev/benchmarks'


def capture_current() -> dict:
    """Capture live state using app modules directly."""
    from benchmark_snapshot import capture
    return capture(label="compare_point")


def _d(val, default=0.0):
    return val if val is not None else default


def _arrow(delta, invert=False):
    if delta is None:
        return "  —  "
    pos = delta > 0
    if invert:
        pos = not pos
    sign = "▲" if pos else "▼"
    color_on  = "\033[92m" if pos else "\033[91m"
    color_off = "\033[0m"
    return f"{color_on}{sign} {delta:+.4f}{color_off}"


def _pct(a, b):
    if b and b != 0:
        return (a - b) / abs(b) * 100
    return None


def compare(baseline: dict, current: dict):
    b = baseline
    c = current

    b_time = b.get("captured_at_local") or b.get("captured_at", "?")
    c_time = c.get("captured_at", "?")

    # Elapsed
    try:
        t0 = datetime.fromisoformat(b["captured_at"])
        t1 = datetime.fromisoformat(c["captured_at"])
        elapsed = t1 - t0
        hours   = elapsed.total_seconds() / 3600
        elapsed_str = f"{int(elapsed.total_seconds()//3600)}h {int((elapsed.total_seconds()%3600)//60)}m"
    except Exception:
        elapsed_str = "?"
        hours = 0

    def _g(d, *keys, default=0.0):
        """Safe nested get with numeric default."""
        v = d
        for k in keys:
            if isinstance(v, dict):
                v = v.get(k)
            else:
                return default
        return v if v is not None else default

    # Deltas — liquid USDT (only tracked metric in new format)
    liq_b   = _g(b, "capital", "liquid_usdt")
    liq_c   = _g(c, "capital", "liquid_usdt")
    liq_d   = liq_c - liq_b
    liq_pct = _pct(liq_c, liq_b) if liq_b else None

    # Futures
    ft_syms_b   = _g(b, "futures", "symbols_traded", default=0)
    ft_syms_c   = _g(c, "futures", "symbols_traded", default=0)
    ft_new      = int(ft_syms_c) - int(ft_syms_b)
    ft_wins_b   = int(_g(b, "futures", "wins", default=0))
    ft_wins_c   = int(_g(c, "futures", "wins", default=0))
    ft_new_wins = ft_wins_c - ft_wins_b
    ft_pnl_b    = _g(b, "futures", "total_pnl")
    ft_pnl_c    = _g(c, "futures", "total_pnl")
    ft_pnl_d    = ft_pnl_c - ft_pnl_b

    # Spot
    sp_closed_b = int(_g(b, "spot", "closed_total", default=0))
    sp_closed_c = int(_g(c, "spot", "closed_total", default=0))
    sp_new      = sp_closed_c - sp_closed_b
    sp_wins_b   = int(_g(b, "spot", "wins", default=0))
    sp_wins_c   = int(_g(c, "spot", "wins", default=0))
    sp_new_wins = sp_wins_c - sp_wins_b
    sp_pnl_b    = _g(b, "spot", "total_pnl")
    sp_pnl_c    = _g(c, "spot", "total_pnl")
    sp_pnl_d    = sp_pnl_c - sp_pnl_b
    sp_wr_b     = _g(b, "spot", "win_rate", default=None)
    sp_wr_c     = _g(c, "spot", "win_rate", default=None)

    fng_b = _g(b, "market", "fng_value", default=None)
    fng_c = _g(c, "market", "fng_value", default=None)

    W = 62
    sep = "=" * W

    print(f"\n{sep}")
    print(f"  BENCHMARK — sesión 5 post-fixes vs ahora")
    print(sep)
    print(f"  Baseline:  {b_time}  [{b.get('label','')}]")
    print(f"  Actual:    {c_time}")
    print(f"  Elapsed:   {elapsed_str}")
    print(sep)

    # Capital líquido
    liq_arrow = "\033[92m▲\033[0m" if liq_d >= 0 else "\033[91m▼\033[0m"
    liq_pct_str = f"({liq_pct:+.2f}%)" if liq_pct is not None else ""
    print(f"\n  CAPITAL LÍQUIDO USDT")
    print(f"    ${liq_b:>10,.2f}  →  ${liq_c:>10,.2f}  {liq_arrow} {liq_d:+.2f} {liq_pct_str}")
    if hours > 0:
        daily_rate = liq_d / hours * 24
        arrow2 = "\033[92m" if daily_rate >= 0 else "\033[91m"
        print(f"    Tasa diaria proyectada: {arrow2}{daily_rate:+.2f} USDT/día\033[0m")

    # Futuros
    print(f"\n  FUTURES SHORTS")
    pnl_arrow = "\033[92m▲\033[0m" if ft_pnl_d >= 0 else "\033[91m▼\033[0m"
    print(f"    Símbolos operados: {ft_syms_b:>4}  →  {ft_syms_c:>4}  (+{ft_new} nuevos, {ft_new_wins} wins)")
    print(f"    PnL realizado:     ${ft_pnl_b:>8.2f}  →  ${ft_pnl_c:>8.2f}  {pnl_arrow} {ft_pnl_d:+.2f} USDT")

    # Spot
    print(f"\n  SPOT TRADES")
    sp_wr_str_b = f"{sp_wr_b*100:.1f}%" if sp_wr_b else "N/A"
    sp_wr_str_c = f"{sp_wr_c*100:.1f}%" if sp_wr_c else "N/A"
    wr_arrow = ""
    if sp_wr_b and sp_wr_c:
        wr_arrow = "\033[92m▲\033[0m" if sp_wr_c > sp_wr_b else "\033[91m▼\033[0m"
    sp_pnl_arrow = "\033[92m▲\033[0m" if sp_pnl_d >= 0 else "\033[91m▼\033[0m"
    print(f"    Cerrados:  {sp_closed_b:>4}  →  {sp_closed_c:>4}  (+{sp_new} trades, {sp_new_wins} wins)")
    print(f"    Win rate:  {sp_wr_str_b:>6}  →  {sp_wr_str_c:>6}  {wr_arrow}")
    print(f"    PnL spot:  ${sp_pnl_b:>8.2f}  →  ${sp_pnl_c:>8.2f}  {sp_pnl_arrow} {sp_pnl_d:+.2f} USDT")

    # F&G
    print(f"\n  MERCADO")
    if fng_b is not None and fng_c is not None:
        fng_d = int(fng_c) - int(fng_b)
        fng_arrow = "\033[92m▲\033[0m" if fng_d >= 0 else "\033[91m▼\033[0m"
        print(f"    F&G:  {fng_b}  →  {fng_c}  {fng_arrow} {fng_d:+d}")

    # Veredicto
    print(f"\n{sep}")
    total_pnl_d = liq_d + ft_pnl_d + sp_pnl_d
    if total_pnl_d > 0:
        print(f"\033[92m  ✅ PnL NETO POSITIVO +${total_pnl_d:.2f} USDT en {elapsed_str}\033[0m")
    elif total_pnl_d == 0:
        print(f"\033[93m  ➡️  SIN CAMBIO en {elapsed_str}\033[0m")
    else:
        print(f"\033[91m  ⚠️  PnL NETO {total_pnl_d:.2f} USDT en {elapsed_str}\033[0m")
    print(sep)

    # Save current as timestamped snapshot
    ts       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    out_path = os.path.join(OUT_DIR, f"snapshot_{ts}.json")
    with open(out_path, "w") as f:
        json.dump(current, f, indent=2, default=str)
    print(f"\n  Snapshot guardado: {out_path}\n")


def main():
    parser = argparse.ArgumentParser(description="Compare system state vs baseline")
    parser.add_argument("--baseline", default=None,
                        help="Path to baseline JSON (default: Dev/benchmarks/baseline.json)")
    args = parser.parse_args()

    baseline_path = args.baseline or os.path.join(OUT_DIR, "baseline.json")
    if not os.path.exists(baseline_path):
        print(f"ERROR: baseline not found at {baseline_path}")
        print("Run benchmark_snapshot.py first to create it.")
        sys.exit(1)

    with open(baseline_path, "r", encoding="utf-8") as f:
        baseline = json.load(f)

    print("Fetching current system state...")
    current = capture_current()
    compare(baseline, current)


if __name__ == "__main__":
    main()

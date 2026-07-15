"""
close_position.py — CLI para cierre manual de una posición SPOT o FUTURES.

Uso básico:
  python Dev/tools/close_position.py --symbol BNBUSDT
  python Dev/tools/close_position.py --symbol BTCUSDT --ghost
  python Dev/tools/close_position.py --symbol SOLUSDT --trade-type futures --side SHORT
  python Dev/tools/close_position.py --symbol BNBUSDT --execute

Por defecto corre en modo PREVIEW (--preview). Usar --execute para operar.
"""
import argparse
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _parse_args():
    p = argparse.ArgumentParser(description="Cierre manual de posición SPOT o FUTURES.")
    p.add_argument("--symbol", required=True, help="Símbolo, ej. BNBUSDT")
    p.add_argument(
        "--trade-type", choices=["spot", "futures", "auto"], default="auto",
        help="Tipo de operación. auto=detectar según DB/Binance (default: auto)"
    )
    p.add_argument(
        "--side", choices=["LONG", "SHORT"], default="SHORT",
        help="Lado del futures (solo aplica para --trade-type futures)"
    )
    p.add_argument(
        "--ghost", action="store_true",
        help="Si el saldo real en Binance es 0, cerrar solo el registro DB como ghost."
    )
    p.add_argument(
        "--execute", action="store_true",
        help="Ejecutar el cierre real. Sin este flag solo muestra preview."
    )
    p.add_argument(
        "--source", default="cli",
        help="Fuente auditada del cierre (default: cli)"
    )
    return p.parse_args()


def _detect_trade_type(symbol: str, components) -> str:
    """Detect if position is spot or futures from DB."""
    db = getattr(components, "db_client", None)
    if db:
        rows = db.execute_query(
            "SELECT metadata FROM trades_open WHERE symbol=%s LIMIT 5", (symbol,)
        )
        for row in rows:
            meta = row.get("metadata") or {}
            if isinstance(meta, str):
                import json as _j
                try:
                    meta = _j.loads(meta)
                except Exception:
                    meta = {}
            if meta.get("trade_type") == "futures":
                return "futures"
    return "spot"


def main():
    args = _parse_args()
    symbol = args.symbol.upper()

    from init_components import get_components
    import manual_position_closer as mpc

    comp = get_components()

    trade_type = args.trade_type
    if trade_type == "auto":
        trade_type = _detect_trade_type(symbol, comp)

    print(f"\n{'='*60}")
    print(f"  CIERRE MANUAL — {symbol} [{trade_type.upper()}]")
    print(f"{'='*60}")

    if args.ghost:
        if not args.execute:
            print("[PREVIEW] Ghost DB close:")
            all_pos = comp.position_manager.get_all_positions()
            pos_list = all_pos.get(symbol, [])
            if not pos_list:
                print("  No hay posición abierta en DB.")
            else:
                for p in pos_list:
                    print(f"  entry={float(p.entry_price):.4f} qty={float(p.quantity):.8f}")
            print("\nEjecuta con --execute para cerrar el registro DB.")
            return
        result = mpc.close_ghost_db(symbol, comp, source=args.source)
        print(json.dumps(result, indent=2))
        return

    if trade_type == "futures":
        if not args.execute:
            result = mpc.preview_futures(symbol, comp)
            print("[PREVIEW FUTURES]")
            print(json.dumps(result, indent=2))
            print("\nEjecuta con --execute para cerrar posición real en Binance.")
        else:
            result = mpc.execute_futures(symbol, args.side, comp, source=args.source)
            print(json.dumps(result, indent=2))
    else:
        if not args.execute:
            result = mpc.preview_spot(symbol, comp)
            print("[PREVIEW SPOT]")
            print(json.dumps(result, indent=2))
            if result.get("topup_needed"):
                print(f"\n  ⚠ Top-up requerido: comprar {result['topup_qty']:.8f} (~${result['topup_usdt']:.2f} USDT)")
            print("\nEjecuta con --execute para cerrar posición real en Binance.")
        else:
            result = mpc.execute_spot(symbol, comp, source=args.source)
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

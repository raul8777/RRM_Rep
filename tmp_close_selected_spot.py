import argparse

from init_components import get_components
from trade_utils import get_symbol_filters, align_quantity_to_step, is_quantity_valid


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Cierra SPOT solo si el estimado neto (incl. comisiones) no es negativo."
    )
    parser.add_argument(
        "--symbols",
        default="ENAUSDT,ADAUSDT,BTCUSDT",
        help="Lista CSV de simbolos objetivo. Ejemplo: ENAUSDT,ADAUSDT",
    )
    parser.add_argument(
        "--close-reason",
        default="manual_close_small_runner",
        help="close_reason para trades_closed",
    )
    parser.add_argument(
        "--commission-rate",
        type=float,
        default=0.001,
        help="Comision round-trip estimada para fallback",
    )
    return parser.parse_args()


def _safe_fill_price(order, fallback_price):
    fill_price = fallback_price
    try:
        fills = order.get("fills", []) if isinstance(order, dict) else []
        if fills:
            fill_price = float(fills[0].get("price", fallback_price))
    except Exception:
        pass
    return fill_price


def main():
    args = _parse_args()
    targets = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    comp = get_components()
    client = comp.binance_client
    pm = comp.position_manager

    all_pos = pm.get_all_positions()
    balances = client.get_account_balance()

    for sym in targets:
        pos_list = all_pos.get(sym, [])
        if not pos_list:
            print(f"{sym}: no abierta en PM")
            continue

        price = float(client.get_symbol_price(sym, use_cache=False))
        est_pnl = 0.0

        for p in pos_list:
            qty = float(p.quantity)
            entry = float(p.entry_price)
            side = str(p.side).upper()
            gross = (price - entry) * qty if side in ("BUY", "LONG") else (entry - price) * qty
            entry_comm = float(getattr(p, "entry_commission", 0.0) or 0.0)
            if entry_comm <= 0:
                entry_comm = qty * entry * args.commission_rate
            exit_comm = qty * price * args.commission_rate
            est_pnl += gross - entry_comm - exit_comm

        if est_pnl < 0:
            print(f"{sym}: SKIP (estimado en perdida) est_pnl={est_pnl:.6f}")
            continue

        base = sym.replace("USDT", "")
        qty_held = float(balances.get(base, 0.0))
        if qty_held <= 0:
            print(f"{sym}: SKIP (sin saldo en wallet)")
            continue

        filters = get_symbol_filters(client, sym)
        qty_sell = align_quantity_to_step(qty_held, filters["step"])
        valid, reason = is_quantity_valid(qty_sell, price, filters)
        if not valid:
            print(f"{sym}: SKIP (filtro exchange) {reason}")
            continue

        order = client.create_market_order(symbol=sym, side="SELL", quantity=qty_sell)
        ok = client.verify_order_filled(order, f"MANUAL_CLOSE {sym}")
        if not ok:
            print(f"{sym}: fallo verificacion de fill")
            continue

        fill_price = _safe_fill_price(order, price)
        closed = pm.close_all_positions_for_symbol(
            symbol=sym,
            exit_price=fill_price,
            close_reason=args.close_reason,
            commission_rate=args.commission_rate,
        )
        print(f"{sym}: CERRADA(s) {len(closed)} | est_pnl_preclose={est_pnl:.6f}")

    print("done")


if __name__ == "__main__":
    main()

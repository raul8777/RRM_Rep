import argparse

from init_components import get_components
from trade_utils import get_symbol_filters, align_quantity_to_step, is_quantity_valid


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Cierra posiciones SPOT dust haciendo top-up para cumplir min_notional y vendiendo todo."
    )
    parser.add_argument(
        "--symbols",
        default="ENAUSDT,ADAUSDT",
        help="Lista CSV de simbolos objetivo. Ejemplo: ENAUSDT,ADAUSDT",
    )
    parser.add_argument(
        "--buffer-usdt",
        type=float,
        default=0.30,
        help="Buffer adicional sobre min_notional para reducir rechazos por variacion de precio.",
    )
    parser.add_argument(
        "--close-reason",
        default="manual_force_close_dust_runner",
        help="close_reason para trades_closed",
    )
    parser.add_argument(
        "--commission-rate",
        type=float,
        default=0.001,
        help="Comision de cierre usada por PM",
    )
    return parser.parse_args()


def safe_get_fill_price(order, fallback_price):
    price = fallback_price
    try:
        fills = order.get("fills", []) if isinstance(order, dict) else []
        if fills:
            price = float(fills[0].get("price", fallback_price))
    except Exception:
        pass
    return price


def main():
    args = _parse_args()
    targets = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    comp = get_components()
    client = comp.binance_client
    pm = comp.position_manager

    for sym in targets:
        print(f"\n=== {sym} ===")
        all_pos = pm.get_all_positions()
        pos_list = all_pos.get(sym, [])
        if not pos_list:
            print("No abierta en PM")
            continue

        filters = get_symbol_filters(client, sym)
        min_notional = float(filters.get("min_notional", 5.0) or 5.0)
        step = float(filters["step"])

        price = float(client.get_symbol_price(sym, use_cache=False))
        base = sym.replace("USDT", "")
        balances = client.get_account_balance()
        qty_held = float(balances.get(base, 0.0))
        notional = qty_held * price
        print(
            f"wallet qty={qty_held:.10f} price={price:.8f} notional={notional:.6f} min_notional={min_notional:.6f}"
        )

        # Si esta por debajo de minimo, compra faltante + buffer para habilitar venta.
        if notional < min_notional:
            need_usdt = (min_notional - notional) + args.buffer_usdt
            # La orden de compra tambien debe cumplir min_notional por si misma.
            buy_usdt = max(need_usdt, min_notional + args.buffer_usdt)
            need_qty = align_quantity_to_step((buy_usdt / price), step)
            if need_qty <= 0:
                print("SKIP: need_qty invalida")
                continue

            buy_notional = need_qty * price
            print(f"buy top-up qty={need_qty:.10f} notional~{buy_notional:.6f}")
            ok_buy, msg_buy = is_quantity_valid(need_qty, price, filters)
            if not ok_buy:
                print(f"SKIP: top-up invalido por filtros ({msg_buy})")
                continue

            buy_order = client.create_market_order(symbol=sym, side="BUY", quantity=need_qty)
            if not client.verify_order_filled(buy_order, f"FORCE_DUST_TOPUP {sym}"):
                print("SKIP: top-up no filled")
                continue
            buy_fill_price = safe_get_fill_price(buy_order, price)
            print(f"top-up filled at {buy_fill_price:.8f}")

        # Refresca balances/precio tras top-up.
        balances = client.get_account_balance()
        price = float(client.get_symbol_price(sym, use_cache=False))
        qty_held = float(balances.get(base, 0.0))
        sell_qty = align_quantity_to_step(qty_held, step)
        ok_sell, msg_sell = is_quantity_valid(sell_qty, price, filters)
        if not ok_sell:
            print(f"SKIP: venta invalida por filtros ({msg_sell})")
            continue

        sell_order = client.create_market_order(symbol=sym, side="SELL", quantity=sell_qty)
        if not client.verify_order_filled(sell_order, f"FORCE_DUST_CLOSE {sym}"):
            print("SKIP: venta no filled")
            continue

        sell_fill_price = safe_get_fill_price(sell_order, price)
        closed = pm.close_all_positions_for_symbol(
            symbol=sym,
            exit_price=sell_fill_price,
            close_reason=args.close_reason,
            commission_rate=args.commission_rate,
        )
        print(f"CERRADA(s) PM={len(closed)} @ {sell_fill_price:.8f}")

    print("\ndone")


if __name__ == "__main__":
    main()

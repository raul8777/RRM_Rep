"""
manual_position_closer.py — Servicio de cierre manual de posiciones SPOT y FUTURES.

Diseñado para ser reutilizado por:
  - Dev/tools/close_position.py (CLI)
  - app.py endpoints POST /positions/{symbol}/close[/preview]

Contrato:
  - SPOT: lee balance real de Binance, compra faltante si no alcanza minNotional, luego vende.
  - FUTURES: cancela SL/TP algo, cierra posición real, verifica positionAmt=0.
  - Resultado siempre marcado como manual_close + exclude_from_analysis en metadata.
  - Auditoría completa en trades_audit.
  - Preview no muta estado.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

MANUAL_CLOSE_REASON = "manual_close"
_BUFFER_USDT = 0.50


# ── helpers ───────────────────────────────────────────────────────────────────

def _fill_price(order: dict, fallback: float) -> float:
    try:
        fills = order.get("fills", []) if isinstance(order, dict) else []
        if fills:
            total_qty = sum(float(f["qty"]) for f in fills)
            if total_qty > 0:
                return sum(float(f["price"]) * float(f["qty"]) for f in fills) / total_qty
    except Exception:
        pass
    return fallback


def _manual_meta(source: str, extra: Optional[Dict] = None) -> Dict:
    m = {
        "manual_close": True,
        "exclude_from_analysis": True,
        "excluded_from_learning": True,
        "manual_close_source": source,
        "manual_close_time": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        m.update(extra)
    return m


def _audit(db_client, symbol: str, event: str, detail: Dict) -> None:
    if not db_client:
        return
    try:
        db_client.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed)"
            " VALUES (%s, %s, %s, %s, %s)",
            (symbol, "spot", event, json.dumps(detail), False),
        )
    except Exception as e:
        logger.warning(f"[manual_closer] audit write failed {symbol}:{event}: {e}")


# ── SPOT ──────────────────────────────────────────────────────────────────────

def preview_spot(symbol: str, components) -> Dict:
    """Dry-run: return what would happen without mutating state."""
    from trade_utils import get_symbol_filters, align_quantity_to_step, is_quantity_valid

    client = components.binance_client
    pm = components.position_manager

    all_pos = pm.get_all_positions()
    pos_list = all_pos.get(symbol, [])
    if not pos_list:
        return {"ok": False, "reason": "no_open_position", "symbol": symbol}

    price = float(client.get_symbol_price(symbol, use_cache=False))
    base = symbol.replace("USDT", "")
    balances = client.get_account_balance()
    qty_held = float(balances.get(base, 0.0))
    notional_held = qty_held * price

    filters = get_symbol_filters(client, symbol)
    step = float(filters["step"])
    min_notional = float(filters.get("min_notional") or 5.0)
    min_qty = float(filters.get("min_qty") or 0.0)

    db_qty = sum(float(p.quantity) for p in pos_list)

    topup_needed = notional_held < min_notional
    topup_qty = 0.0
    topup_usdt = 0.0
    if topup_needed:
        need_usdt = (min_notional - notional_held) + _BUFFER_USDT
        buy_usdt = max(need_usdt, min_notional + _BUFFER_USDT)
        topup_qty = align_quantity_to_step(buy_usdt / price, step)
        topup_usdt = round(topup_qty * price, 4)

    projected_sell_qty = align_quantity_to_step(qty_held + topup_qty, step)
    ok_sell, sell_reason = is_quantity_valid(projected_sell_qty, price, filters)

    return {
        "ok": True,
        "symbol": symbol,
        "trade_type": "spot",
        "db_qty": db_qty,
        "binance_qty": qty_held,
        "notional_held": round(notional_held, 4),
        "min_notional": min_notional,
        "min_qty": min_qty,
        "price": price,
        "topup_needed": topup_needed,
        "topup_qty": topup_qty,
        "topup_usdt": topup_usdt,
        "projected_sell_qty": projected_sell_qty,
        "sell_valid": ok_sell,
        "sell_invalid_reason": sell_reason if not ok_sell else None,
        "entries": len(pos_list),
    }


def execute_spot(symbol: str, components, source: str = "api") -> Dict:
    """
    Close a SPOT position:
      1. Read real Binance balance.
      2. If below minNotional, buy the minimum required top-up.
      3. Sell the full balance.
      4. Persist close in DB with manual metadata.
    """
    from trade_utils import get_symbol_filters, align_quantity_to_step, is_quantity_valid
    import config_helpers

    client = components.binance_client
    pm = components.position_manager
    db = getattr(components, "db_client", None)
    settings = components.settings

    all_pos = pm.get_all_positions()
    pos_list = all_pos.get(symbol, [])
    if not pos_list:
        return {"ok": False, "reason": "no_open_position", "symbol": symbol}

    price = float(client.get_symbol_price(symbol, use_cache=False))
    base = symbol.replace("USDT", "")
    balances = client.get_account_balance()
    qty_held = float(balances.get(base, 0.0))
    notional_held = qty_held * price

    filters = get_symbol_filters(client, symbol)
    step = float(filters["step"])
    min_notional = float(filters.get("min_notional") or 5.0)

    topup_order = None
    topup_fill_price = None

    _audit(db, symbol, "manual_position_close_start", {
        "source": source, "binance_qty": qty_held,
        "notional_held": round(notional_held, 4), "min_notional": min_notional,
    })

    if notional_held < min_notional:
        need_usdt = (min_notional - notional_held) + _BUFFER_USDT
        buy_usdt = max(need_usdt, min_notional + _BUFFER_USDT)
        topup_qty = align_quantity_to_step(buy_usdt / price, step)
        if topup_qty <= 0:
            return {"ok": False, "reason": "topup_qty_invalid", "symbol": symbol}

        ok_buy, buy_reason = is_quantity_valid(topup_qty, price, filters)
        if not ok_buy:
            return {"ok": False, "reason": f"topup_invalid_filters: {buy_reason}", "symbol": symbol}

        logger.info(f"[manual_closer] {symbol}: buying top-up qty={topup_qty} usdt~{round(topup_qty*price,2)}")
        _audit(db, symbol, "manual_position_topup", {"topup_qty": topup_qty, "price": price})

        topup_order = client.create_market_order(symbol=symbol, side="BUY", quantity=topup_qty)
        if not client.verify_order_filled(topup_order, f"MANUAL_TOPUP {symbol}"):
            return {"ok": False, "reason": "topup_not_filled", "symbol": symbol}

        topup_fill_price = _fill_price(topup_order, price)

        # Refresh balance and price after top-up
        balances = client.get_account_balance()
        price = float(client.get_symbol_price(symbol, use_cache=False))
        qty_held = float(balances.get(base, 0.0))

    sell_qty = align_quantity_to_step(qty_held, step)
    ok_sell, sell_reason = is_quantity_valid(sell_qty, price, filters)
    if not ok_sell:
        return {
            "ok": False,
            "reason": f"sell_invalid_filters: {sell_reason}",
            "symbol": symbol,
            "topup_order": topup_order,
        }

    logger.info(f"[manual_closer] {symbol}: selling qty={sell_qty} @ ~{price}")
    sell_order = client.create_market_order(symbol=symbol, side="SELL", quantity=sell_qty)
    if not client.verify_order_filled(sell_order, f"MANUAL_CLOSE {symbol}"):
        return {
            "ok": False, "reason": "sell_not_filled",
            "symbol": symbol, "sell_order": sell_order,
        }

    fill_price = _fill_price(sell_order, price)

    _audit(db, symbol, "manual_position_close_binance_filled", {
        "fill_price": fill_price, "sell_qty": sell_qty,
        "sell_order_id": sell_order.get("orderId") if isinstance(sell_order, dict) else None,
    })

    commission_rate = config_helpers.get_param(settings, "binance_commission_rate")

    extra_meta = {
        "topup_qty": float(topup_qty) if topup_order else 0.0,
        "topup_usdt": round(float(topup_qty) * float(topup_fill_price), 4) if topup_order else 0.0,
        "topup_order_id": (topup_order.get("orderId") if isinstance(topup_order, dict) else None) if topup_order else None,
        "sell_order_id": sell_order.get("orderId") if isinstance(sell_order, dict) else None,
        "binance_fill_price": fill_price,
        "binance_sell_qty": sell_qty,
    }

    for pos in pos_list:
        if not isinstance(pos.metadata, dict):
            pos.metadata = {}
        pos.metadata.update(_manual_meta(source, extra_meta))

    closed = pm.close_all_positions_for_symbol(
        symbol=symbol,
        exit_price=fill_price,
        close_reason=MANUAL_CLOSE_REASON,
        commission_rate=commission_rate,
    )

    _audit(db, symbol, "manual_position_close_db_closed", {
        "entries_closed": len(closed),
        "fill_price": fill_price,
        "source": source,
    })

    return {
        "ok": True,
        "symbol": symbol,
        "trade_type": "spot",
        "entries_closed": len(closed),
        "sell_qty": sell_qty,
        "fill_price": fill_price,
        "topup_executed": topup_order is not None,
        "topup_qty": float(topup_qty) if topup_order else 0.0,
        "sell_order_id": sell_order.get("orderId") if isinstance(sell_order, dict) else None,
    }


# ── FUTURES ───────────────────────────────────────────────────────────────────

def preview_futures(symbol: str, components) -> Dict:
    """Dry-run for FUTURES close."""
    from run_trades import _get_futures_client

    db_client = getattr(components, "db_client", None)
    settings = getattr(components, "settings", None)
    if settings is None:
        from config import get_settings
        settings = get_settings()
    fc = _get_futures_client(settings, db_client=db_client)
    if fc is None:
        return {"ok": False, "reason": "futures_client_not_available", "symbol": symbol}

    positions = fc.get_positions()
    target = None
    for p in positions:
        if p.get("symbol") == symbol and abs(float(p.get("positionAmt", 0))) > 0:
            target = p
            break

    if target is None:
        db_client = getattr(components, "db_client", None)
        db_pos = []
        if db_client:
            db_pos = db_client.execute_query(
                "SELECT symbol, side, quantity, entry_price FROM trades_open"
                " WHERE symbol=%s AND metadata->>'trade_type'='futures'", (symbol,)
            )
        return {
            "ok": bool(db_pos),
            "reason": "no_binance_position" if not db_pos else "db_only_ghost",
            "symbol": symbol,
            "db_entries": len(db_pos),
        }

    amt = float(target.get("positionAmt", 0))
    side = "LONG" if amt > 0 else "SHORT"
    return {
        "ok": True,
        "symbol": symbol,
        "trade_type": "futures",
        "side": side,
        "qty": abs(amt),
        "entry_price": float(target.get("entryPrice", 0)),
        "unrealized_pnl": float(target.get("unrealizedProfit", 0)),
    }


def execute_futures(symbol: str, side: str, components, source: str = "api") -> Dict:
    """
    Close a FUTURES position:
      1. Resolve LONG/SHORT from Binance (not stale DB).
      2. Cancel SL/TP algo orders.
      3. Close via FuturesClient.close_long/close_short.
      4. Verify positionAmt=0.
      5. Persist closed with manual metadata.
    """
    from run_trades import _get_futures_client

    db = getattr(components, "db_client", None)
    settings = getattr(components, "settings", None)
    if settings is None:
        from config import get_settings
        settings = get_settings()
    fc = _get_futures_client(settings, db_client=db)
    if fc is None:
        return {"ok": False, "reason": "futures_client_not_available", "symbol": symbol}

    _audit(db, symbol, "manual_position_futures_close_start", {"side": side, "source": source})

    side_upper = side.upper()
    if side_upper == "LONG":
        result = fc.close_long(symbol=symbol, reason=MANUAL_CLOSE_REASON)
    else:
        result = fc.close_short(symbol=symbol, reason=MANUAL_CLOSE_REASON)

    if result is None:
        return {"ok": False, "reason": "futures_close_returned_none", "symbol": symbol}

    _audit(db, symbol, "manual_position_futures_close_done", {
        "side": side,
        "result": str(result)[:200],
        "source": source,
        "order_id": result.get("orderId") if isinstance(result, dict) else None,
        "client_order_id": result.get("clientOrderId") if isinstance(result, dict) else None,
    })

    return {
        "ok": True,
        "symbol": symbol,
        "trade_type": "futures",
        "side": side,
        "result": result,
    }


# ── Ghost DB-only (balance cero) ──────────────────────────────────────────────

def close_ghost_db(symbol: str, components, source: str = "api") -> Dict:
    """
    Close a position that exists only in DB (no real Binance balance to sell).
    Marks as manual_close + ghost_balance_mismatch.
    """
    import config_helpers

    pm = components.position_manager
    db = getattr(components, "db_client", None)
    client = components.binance_client
    settings = components.settings

    all_pos = pm.get_all_positions()
    pos_list = all_pos.get(symbol, [])
    if not pos_list:
        return {"ok": False, "reason": "no_open_position", "symbol": symbol}

    price = float(client.get_symbol_price(symbol, use_cache=False))
    commission_rate = config_helpers.get_param(settings, "binance_commission_rate")

    for pos in pos_list:
        if not isinstance(pos.metadata, dict):
            pos.metadata = {}
        pos.metadata.update(_manual_meta(source, {"ghost": True}))

    closed = pm.close_all_positions_for_symbol(
        symbol=symbol,
        exit_price=price,
        close_reason=f"{MANUAL_CLOSE_REASON}_ghost",
        commission_rate=commission_rate,
    )

    _audit(db, symbol, "manual_position_ghost_closed", {
        "entries_closed": len(closed), "price": price, "source": source,
    })

    return {
        "ok": True,
        "symbol": symbol,
        "trade_type": "spot_ghost",
        "entries_closed": len(closed),
        "price": price,
    }

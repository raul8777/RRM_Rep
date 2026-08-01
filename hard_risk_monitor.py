from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List

from config_helpers import get_param
from trade_utils import get_binance_sell_qty, get_symbol_filters, is_quantity_valid

# PIS: Position Intelligence System
from position_intelligence import (
    PIMAction,
    evaluate_position_intelligence,
    has_behavior_changed,
    invalidate_sr_cache,
    refresh_sr_levels,
    update_sr_behavior,
    compute_sr_aware_trail_distance,
)


_lock = threading.Lock()
_symbols_in_progress: set[str] = set()

# s107: separate lock/in-progress set for the futures safety-net check, so a
# slow spot cycle never blocks futures (or vice versa).
_futures_lock = threading.Lock()
_futures_keys_in_progress: set[str] = set()


def _try_breakeven(manager, settings, db_client, symbol, pos, price, commission_rate):
    """Move SL to net breakeven if position pnl >= BE threshold.

    Runs every 2s from the hard-risk monitor thread, independent of the
    3-5 min trade cycle, so gains are protected even between cycles.
    Returns True if SL was moved.
    """
    try:
        be_enabled = bool(getattr(settings, "hard_risk_breakeven_enabled", True))
        if not be_enabled:
            return False
        entry = float(pos.entry_price or 0.0)
        if entry <= 0:
            return False
        pnl_pct_gross = (price - entry) / entry
        if pnl_pct_gross <= 0:
            return False
        slippage = float(get_param(settings, "slippage_pct", 0.00052) or 0.0)
        net_be_pct = 2.0 * commission_rate + slippage
        pnl_pct_net = pnl_pct_gross - net_be_pct
        # Use the most aggressive (smallest) BE threshold across regimes
        rs_be = float(get_param(settings, "range_scalp_breakeven_pct", 0.40) or 0.40)
        rs_be = min(rs_be, 0.15)
        sb_be = float(get_param(settings, "spot_bull_breakeven_pct", 0.25) or 0.25)
        sb_be = min(sb_be, 0.50)
        be_threshold = min(rs_be, sb_be) / 100.0
        if pnl_pct_net < be_threshold:
            return False
        net_be_price = entry * (1.0 + net_be_pct)
        cur_sl = float(pos.stop_loss or 0.0)
        if cur_sl >= net_be_price:
            return False
        manager.update_position_sl(pos.position_id, round(net_be_price, 8))
        if db_client:
            db_client.execute_update(
                "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
                (
                    symbol,
                    "spot",
                    "hard_risk_breakeven",
                    json.dumps({
                        "position_id": str(pos.position_id),
                        "entry_price": entry,
                        "current_price": price,
                        "pnl_pct_net": round(pnl_pct_net * 100, 4),
                        "be_threshold_pct": round(be_threshold * 100, 4),
                        "new_sl": round(net_be_price, 8),
                        "old_sl": cur_sl,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }),
                    False,
                ),
            )
        return True
    except Exception:
        return False


def _evaluate_pim_spot(client, manager, pos_list, symbol, price, settings, db_client):
    """Run PIM evaluation for SPOT positions. Updates metadata with behavioral data + flags.

    SPOT positions are simpler: they are always LONG, and metadata is
    stored per-position via manager.update_position_metadata().
    """
    if not pos_list:
        return

    def _fetch_klines(sym, interval, limit):
        try:
            return client.get_klines(symbol=sym, interval=interval, limit=limit) or []
        except Exception:
            return []

    sr_levels = refresh_sr_levels(symbol, _fetch_klines, price)
    if not sr_levels:
        return

    for pos in pos_list:
        entry = float(pos.entry_price or 0.0)
        if entry <= 0:
            continue

        # Get existing metadata
        meta = {}
        if hasattr(pos, 'metadata') and pos.metadata:
            meta = dict(pos.metadata) if isinstance(pos.metadata, dict) else {}
        smart_meta = meta.get('_smart_meta') or {}

        # Update behavioral tracking
        sr_behavior = update_sr_behavior(smart_meta, price, sr_levels, "LONG")

        old_behavior = smart_meta.get('sr_behavior')
        if not has_behavior_changed(old_behavior, sr_behavior):
            continue

        smart_meta['sr_behavior'] = sr_behavior

        # Evaluate PIM
        action = evaluate_position_intelligence(
            entry_price=entry,
            current_price=price,
            side="LONG",
            sr_levels=sr_levels,
            sr_behavior=sr_behavior,
            peak_pnl_pct=float(smart_meta.get('peak_pnl_pct', 0.0) or 0.0),
            pnl_velocity=None,
            breakeven_done=bool(smart_meta.get('breakeven_done', False)),
            trailing_active=bool(smart_meta.get('trail_best_price')),
        )

        # Store flag if significant
        if action.confidence >= 0.5:
            if action.action == PIMAction.FLAG_EXIT:
                smart_meta['pim_flag_exit'] = action.to_dict()
            elif action.action == PIMAction.TIGHTEN:
                smart_meta['pim_state'] = action.to_dict()
            elif action.action == PIMAction.HOLD:
                smart_meta['pim_state'] = action.to_dict()

        # Persist updated metadata
        meta['_smart_meta'] = smart_meta
        try:
            manager.update_position_metadata(pos.position_id, meta)
        except Exception:
            pass

        # Audit
        if db_client and action.confidence >= 0.5:
            try:
                db_client.execute_update(
                    "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
                    (symbol, "spot", f"pim_{action.action}", json.dumps(action.to_dict()), False),
                )
            except Exception:
                pass


def monitor_spot_hard_stops_once(components: Any) -> List[Dict[str, Any]]:
    if not _lock.acquire(blocking=False):
        return []
    closed: List[Dict[str, Any]] = []
    try:
        client = components.binance_client
        manager = components.position_manager
        settings = components.settings
        db_client = getattr(components, "db_client", None)
        positions = manager.get_all_positions() if manager else {}
        max_loss = float(get_param(settings, "max_loss_usd_per_trade", 18.0) or 0.0)
        commission_rate = float(get_param(settings, "binance_commission_rate", 0.001) or 0.001)
        for symbol, pos_list in list(positions.items()):
            if not pos_list or symbol in _symbols_in_progress:
                continue
            try:
                price = float(client.get_symbol_price(symbol, use_cache=False))
            except TypeError:
                price = float(client.get_symbol_price(symbol))
            breached = []
            for pos in pos_list:
                entry = float(pos.entry_price or 0.0)
                qty = float(pos.quantity or 0.0)
                if entry <= 0 or qty <= 0:
                    continue
                # Activate breakeven before SL breach check so gains are protected
                _try_breakeven(manager, settings, db_client, symbol, pos, price, commission_rate)
                saved_sl = float(pos.stop_loss or 0.0)
                usd_sl = entry - (max_loss / qty) if max_loss > 0 else 0.0
                effective_sl = max(saved_sl, usd_sl)
                if effective_sl > 0 and price <= effective_sl:
                    breached.append({"position_id": str(pos.position_id), "effective_sl": effective_sl})
            if not breached:
                # ── PIS: evaluate position intelligence for SPOT (no SL breached) ──
                if bool(get_param(settings, "position_intelligence_enabled", True)):
                    try:
                        _evaluate_pim_spot(client, manager, pos_list, symbol, price, settings, db_client)
                    except Exception:
                        pass  # PIM must never break the safety-net loop
                continue
            _symbols_in_progress.add(symbol)
            try:
                full_qty = sum(float(pos.quantity or 0.0) for pos in pos_list)
                sell_check = get_binance_sell_qty(client, symbol, full_qty, price)
                qty = float(sell_check.get("qty_to_sell", 0.0) or 0.0)
                if sell_check.get("is_ghost") or sell_check.get("is_dust") or qty <= 0:
                    continue
                filters = get_symbol_filters(client, symbol)
                valid, reason = is_quantity_valid(qty, price, filters)
                if not valid:
                    raise RuntimeError(reason)
                order = client.create_market_order(symbol=symbol, side="SELL", quantity=qty)
                fill = client.verify_and_fetch_fills(order, f"HARD_RISK SELL {symbol}")
                if not fill:
                    raise RuntimeError("fill_not_confirmed")
                sold_qty = float(fill.get("qty", qty))
                fill_price = float(fill.get("avg_price", price))
                manager.close_all_positions_for_symbol(
                    symbol,
                    fill_price,
                    "hard_risk_stop",
                    commission_rate,
                    exit_context={
                        "exit_order_id": str(fill.get("order_id", "")),
                        "exit_filled_qty": sold_qty,
                        "exit_avg_price": fill_price,
                        "exit_commission": float(fill.get("commission", 0.0)),
                        "binance_exit_confirmed": True,
                    },
                    qty_closed=sold_qty,
                    exit_fill=fill,
                )
                detail = {
                    "symbol": symbol,
                    "price": price,
                    "fill_price": fill_price,
                    "sold_qty": sold_qty,
                    "breached": breached,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                if db_client:
                    db_client.execute_update(
                        "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
                        (symbol, "spot", "hard_risk_stop", json.dumps(detail), True),
                    )
                closed.append(detail)
            finally:
                _symbols_in_progress.discard(symbol)
    finally:
        _lock.release()
    return closed


def _evaluate_pim_futures(futures_client, pos, price, settings, min_conf, emerg_conf, db_client):
    """Run PIM evaluation for a single futures position.

    Returns action dict if confidence >= min_conf, else None.
    Refreshes S/R levels from cache (30s TTL, 1 API call per symbol when stale).
    Updates behavioral tracking in _smart_meta['sr_behavior'].
    """
    symbol = pos.symbol
    smart_meta = pos._smart_meta or {}

    # Fetch klines via futures client (1m, 150 candles, weight=1)
    def _fetch_klines(sym, interval, limit):
        try:
            return futures_client._client.get_klines(symbol=sym, interval=interval, limit=limit) or []
        except Exception:
            return []

    sr_levels = refresh_sr_levels(symbol, _fetch_klines, price)
    if not sr_levels:
        return None

    # Update behavioral tracking
    sr_behavior = update_sr_behavior(smart_meta, price, sr_levels, pos.side)

    # Only persist when meaningful state changes (avoid noisy DB writes)
    old_behavior = smart_meta.get('sr_behavior')
    if has_behavior_changed(old_behavior, sr_behavior):
        if not hasattr(pos, '_smart_meta') or pos._smart_meta is None:
            pos._smart_meta = {}
        pos._smart_meta['sr_behavior'] = sr_behavior
        try:
            futures_client._persist_smart_meta(pos)
        except Exception:
            pass

    # Evaluate position intelligence
    action = evaluate_position_intelligence(
        entry_price=float(pos.entry_price or 0),
        current_price=price,
        side=pos.side,
        sr_levels=sr_levels,
        sr_behavior=sr_behavior,
        peak_pnl_pct=float(smart_meta.get('peak_pnl_pct', 0.0) or 0.0),
        pnl_velocity=float(smart_meta.get('pnl_velocity_1h') or 0) if smart_meta.get('pnl_velocity_1h') else None,
        breakeven_done=bool(smart_meta.get('breakeven_done', False)),
        trailing_active=bool(smart_meta.get('trail_best_price')),
    )

    if action.confidence < min_conf:
        return None

    result = action.to_dict()

    # Handle TIGHTEN: adjust trailing stop (SL never retrocedes)
    if action.action == PIMAction.TIGHTEN and action.confidence >= 0.5:
        try:
            _tighten_sl_from_pim(futures_client, pos, price, sr_levels, settings)
        except Exception:
            pass

    # Handle FLAG_EXIT: write flag for next trade cycle to act on
    if action.action == PIMAction.FLAG_EXIT and action.confidence >= 0.5:
        try:
            if not hasattr(pos, '_smart_meta') or pos._smart_meta is None:
                pos._smart_meta = {}
            pos._smart_meta['pim_flag_exit'] = result
            futures_client._persist_smart_meta(pos)
        except Exception:
            pass

    # Audit significant PIM actions
    if db_client and action.confidence >= 0.5:
        try:
            db_client.execute_update(
                "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
                (symbol, "futures", f"pim_{action.action}", json.dumps(result), False),
            )
        except Exception:
            pass

    # Only return for EMERGENCY actions (caller handles the close)
    if action.action == PIMAction.EMERGENCY and action.confidence >= emerg_conf:
        return result
    return None


def _tighten_sl_from_pim(futures_client, pos, price, sr_levels, settings):
    """Tighten trailing stop based on S/R proximity. SL NEVER retrocedes."""
    is_long = str(pos.side).upper() == "LONG"
    atr_pct = sr_levels.get('atr_pct', 1.0) / 100.0

    if is_long:
        resistance = sr_levels.get('resistance_level')
        if resistance and resistance > price:
            dist_to_r = (resistance - price) / price
            # Trail distance = half distance to resistance, floor 0.3 ATR
            new_trail_dist = max(dist_to_r * 0.5, atr_pct * 0.3)
            implied_new_sl = price * (1 - new_trail_dist)
            cur_sl = float(pos.sl_price or 0.0)
            if implied_new_sl > cur_sl and cur_sl > 0:
                try:
                    futures_client.update_sl_tp(pos.symbol, sl_price=implied_new_sl, current_price=price)
                except Exception:
                    pass
    else:
        support = sr_levels.get('support_level')
        if support and support < price and support > 0:
            dist_to_s = (price - support) / price
            new_trail_dist = max(dist_to_s * 0.5, atr_pct * 0.3)
            implied_new_sl = price * (1 + new_trail_dist)
            cur_sl = float(pos.sl_price or 0.0)
            if cur_sl > 0 and implied_new_sl < cur_sl:
                try:
                    futures_client.update_sl_tp(pos.symbol, sl_price=implied_new_sl, current_price=price)
                except Exception:
                    pass


def monitor_futures_hard_stops_once(components: Any) -> List[Dict[str, Any]]:
    """Lightweight 2s safety-net check for futures positions (s107).

    Mirrors `monitor_spot_hard_stops_once` (s99d) for futures: a bare
    `is_sl_hit()`/`is_tp_hit()` check + immediate close, run every 2s
    independently of the 3-45min trade cycle. Deliberately does NOT duplicate
    `FuturesClient.check_positions()`'s heavier logic (trailing, Smart-TP-extend
    momentum evaluation, SSO scale-outs) — that stays on its normal cadence in
    the trade cycle; this loop exists purely so the "software" SL/TP has a real
    chance to close the position before Binance's own algo order does.

    Now also includes Position Intelligence System (PIS) evaluation when no
    SL/TP is hit: evaluates S/R structure and behavioral evidence every 2s.

    Root cause this closes: before s107, futures positions were only checked
    for SL/TP hits once per trade cycle (3-45 min depending on regime), while
    Binance's real-time matching engine watches its own algo order
    continuously — see calibracion_entradas-a20ab8-v3.md s107 for the
    quantified impact (`binance_unidentified_close`, 23 closes / -$356.86 in
    7 days).
    """
    if not _futures_lock.acquire(blocking=False):
        return []
    closed: List[Dict[str, Any]] = []
    try:
        settings = getattr(components, "settings", None)
        if settings is None or not bool(get_param(settings, "hard_risk_monitor_enabled", True)):
            return []
        if not bool(get_param(settings, "futures_enabled", True)):
            return []
        try:
            from run_trades import _get_futures_client
        except Exception as _import_err:
            print(f"[HardRisk/Futures] could not import futures client accessor: {_import_err}")
            return []
        futures_client = _get_futures_client(settings, db_client=getattr(components, "db_client", None))
        if futures_client is None:
            return []
        pim_enabled = bool(get_param(settings, "position_intelligence_enabled", True))
        pim_min_conf = float(get_param(settings, "position_intelligence_min_confidence", 0.3) or 0.3)
        pim_emerg_conf = float(get_param(settings, "position_intelligence_emergency_confidence", 0.7) or 0.7)
        db_client = getattr(components, "db_client", None)
        positions = list(futures_client._open_positions.items())
        for key, pos in positions:
            if pos.closed or key in _futures_keys_in_progress:
                continue
            try:
                price = futures_client.get_price(pos.symbol)
            except Exception:
                continue
            hit_sl = pos.is_sl_hit(price)
            hit_tp = False if hit_sl else pos.is_tp_hit(price)
            if hit_sl or hit_tp:
                _futures_keys_in_progress.add(key)
                try:
                    reason = "stop_loss" if hit_sl else "take_profit"
                    result = (
                        futures_client.close_short(pos.symbol, reason=reason)
                        if pos.side == "SHORT"
                        else futures_client.close_long(pos.symbol, reason=reason)
                    )
                    if result:
                        closed.append({
                            "symbol": pos.symbol,
                            "side": pos.side,
                            "reason": reason,
                            "pnl": result.get("pnl") if isinstance(result, dict) else None,
                        })
                        print(f"[HardRisk/Futures] {pos.symbol} {pos.side}: closed via fast safety-net ({reason})")
                except Exception as _close_err:
                    print(f"[HardRisk/Futures] {pos.symbol} {pos.side}: close attempt failed: {_close_err}")
                finally:
                    _futures_keys_in_progress.discard(key)
                continue

            # ── PIS: Position Intelligence evaluation (no SL/TP hit) ─────────
            if not pim_enabled:
                continue
            try:
                _pim_result = _evaluate_pim_futures(
                    futures_client, pos, price, settings,
                    pim_min_conf, pim_emerg_conf, db_client,
                )
                if _pim_result and _pim_result.get("action") == PIMAction.EMERGENCY:
                    _futures_keys_in_progress.add(key)
                    try:
                        reason = f"pim_emergency_{_pim_result.get('reason', 'structural_break')}"
                        result = (
                            futures_client.close_short(pos.symbol, reason=reason)
                            if pos.side == "SHORT"
                            else futures_client.close_long(pos.symbol, reason=reason)
                        )
                        if result:
                            closed.append({
                                "symbol": pos.symbol,
                                "side": pos.side,
                                "reason": reason,
                                "pnl": result.get("pnl") if isinstance(result, dict) else None,
                                "pim_confidence": _pim_result.get("confidence"),
                            })
                            print(f"[HardRisk/Futures/PIM] {pos.symbol} {pos.side}: EMERGENCY close ({reason}, conf={_pim_result.get('confidence', 0):.2f})")
                    except Exception as _close_err:
                        print(f"[HardRisk/Futures/PIM] {pos.symbol} {pos.side}: emergency close failed: {_close_err}")
                    finally:
                        _futures_keys_in_progress.discard(key)
            except Exception as _pim_err:
                # PIM must never break the safety-net loop
                pass
    finally:
        _futures_lock.release()
    return closed

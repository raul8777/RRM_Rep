path = '/app/run_trades.py'
with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

start_idx = None
for i, line in enumerate(lines):
    if '# Partial close for SPOT' in line:
        start_idx = i - 1
        break

if start_idx is None:
    print('start marker not found')
    raise SystemExit(1)

end_idx = None
for i in range(start_idx, len(lines)):
    if 'except Exception as _sso_s_err:' in lines[i]:
        end_idx = i + 1
        break

print(f'replacing lines {start_idx+1}-{end_idx}')

new_block = '''                                else:
                                # Partial close for SPOT
                                try:
                                    _sso_s_filters = get_symbol_filters(client, symbol)
                                    _sso_s_full_qty = float(pos.quantity)
                                    _sso_s_sell_qty = align_quantity_to_step(
                                        _sso_s_full_qty * _sso_s_action.close_pct, _sso_s_filters["step"])
                                    _sso_s_valid, _ = is_quantity_valid(_sso_s_sell_qty, current_price, _sso_s_filters)
                                    if _sso_s_valid and _sso_s_sell_qty > 0:
                                        _sso_s_order = client.create_market_order(symbol=symbol, side="SELL", quantity=_sso_s_sell_qty)
                                        if not client.verify_order_filled(_sso_s_order, f"SSO/SPOT SELL {symbol}"):
                                            print(f"[WARN] SSO/SPOT SELL {symbol}: order NOT confirmed filled — skipping")
                                            continue
                                        _sso_s_remaining = align_quantity_to_step(
                                            _sso_s_full_qty - _sso_s_sell_qty, _sso_s_filters["step"])
                                        # Dust guard: if remaining < min_notional, treat as full close
                                        if 0 < _sso_s_remaining * current_price < _sso_s_filters.get('min_notional', 1.0):
                                            print(f"[SSO/SPOT] {symbol}: remaining ${_sso_s_remaining * current_price:.4f} < min_notional — converting to full close")
                                            _sso_s_sell_qty = _sso_s_full_qty
                                            _sso_s_remaining = 0.0
                                        # Persist the partial close in DB via PositionManager so a
                                        # trades_closed row is created and quantity/commission update atomically.
                                        _sso_s_closed = position_manager.close_position(
                                            symbol=symbol,
                                            exit_price=current_price,
                                            close_reason='sso_partial_close',
                                            commission_rate=commission_rate,
                                            position_id=pos.position_id,
                                            qty_closed=_sso_s_sell_qty,
                                            exit_context={'regime_mode': regime, 'profile_label': str(getattr(settings, 'current_regime_profile_label', '') or '').upper()},
                                        )
                                        if _sso_s_closed is None:
                                            print(f"[WARN] SSO/SPOT {symbol}: position_manager.close_position returned None — falling back to metadata-only update")
                                            try:
                                                position_manager.update_position_quantity(pos.position_id, _sso_s_remaining)
                                            except Exception:
                                                pass
                                        # Update SSO metadata (level tracking, last trigger) on the residual position
                                        metadata = getattr(pos, 'metadata', {}) or {}
                                        if not isinstance(metadata, dict):
                                            metadata = {}
                                        metadata['sso_last_trigger'] = _sso_s_action.trigger
                                        metadata['sso_last_reason'] = _sso_s_action.reason
                                        metadata['scale_out_done'] = True
                                        _sso_s_lvl = extract_sso_level_from_reason(_sso_s_action.reason)
                                        if _sso_s_lvl:
                                            _sso_s_done = metadata.get('sso_levels_done', [])
                                            if _sso_s_lvl not in _sso_s_done:
                                                _sso_s_done.append(_sso_s_lvl)
                                            metadata['sso_levels_done'] = _sso_s_done
                                        try:
                                            position_manager.update_position_metadata(pos.position_id, metadata)
                                            if _sso_s_remaining > 0:
                                                position_manager.update_position_sl(pos.position_id, round(entry_price, 8))
                                        except Exception:
                                            pass
                                        print(f"[SSO/SPOT] {symbol}: {_sso_s_action.trigger} "
                                              f"SCALE {_sso_s_action.close_pct*100:.0f}% "
                                              f"qty={_sso_s_sell_qty} | {_sso_s_action.detail}")
                                        closed.append({"symbol": symbol, "reason": _sso_s_action.reason,
                                                       "price": current_price, "order": _sso_s_order,
                                                       "sso_trigger": _sso_s_action.trigger})
                                        # s73 E1: trazabilidad SSO partial-close en trades_audit
                                        try:
                                            if _db_client_ref:
                                                _db_client_ref.execute_update(
                                                    "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
                                                    "VALUES (%s, %s, %s, %s, %s)",
                                                    (
                                                        symbol, 'spot', 'sso_partial_close',
                                                        __import__('json').dumps({
                                                            'trigger': _sso_s_action.trigger,
                                                            'reason': _sso_s_action.reason,
                                                            'close_pct': round(_sso_s_action.close_pct, 4),
                                                            'qty_sold': float(_sso_s_sell_qty),
                                                            'qty_remaining': float(_sso_s_remaining),
                                                            'pnl_pct': round(pnl_pct * 100, 4),
                                                            'pnl_usdt': round(_sso_sym_pnl_usdt, 4),
                                                            'diagnosis': (_sso_s_action.diagnosis or '')[:500],
                                                            'regime': regime,
                                                            'profile': str(getattr(settings, 'current_regime_profile_label', '') or '').upper(),
                                                            'ts': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
                                                        }),
                                                        False,
                                                    )
                                                )
                                        except Exception:
                                            pass
                                        continue  # partial done, move to next position
                                except Exception as _sso_s_err:
                                    print(f"[SSO/SPOT] {symbol} partial close error: {_sso_s_err}")
'''

new_lines = new_block.splitlines(keepends=True)
if not new_lines[-1].endswith('\n'):
    new_lines[-1] += '\n'

with open(path, 'w', encoding='utf-8') as f:
    f.writelines(lines[:start_idx] + new_lines + lines[end_idx:])
print('patched')

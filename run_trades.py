import time
from datetime import datetime, timezone
from typing import Dict, List

import error_collector as ec
from binance_client import BinanceClient
from news_client import NewsClient
from signal_store import get_signals, add_signal, mark_signal_as_processed, update_signal_decision, expire_stale_signals
from strategy import rank_buy_signals, rank_with_technical, normalize_symbol, rank_for_portfolio_manager
from analysis_cache import analysis_cache
from autonomous_signals import AutonomousSignalGenerator
from trade_utils import align_quantity_to_step, get_symbol_filters, is_quantity_valid, compute_commission
import pnl_service


# Symbols confirmed invalid on Binance (delisted) — skip without API call
_INVALID_SYMBOLS: set = {"EONUSDT", "ADDUSDT", "MEETONEUSDT", "ATDUSDT", "EOPUSDT", "ETHWUSDT"}


def _get_news_scores(symbols: List[str], news_client: NewsClient, settings) -> Dict[str, float]:
    if not symbols:
        return {}

    recent = news_client.get_recent_news(symbols, settings.news_lookback_hours)
    scores: Dict[str, float] = {}
    for article in recent:
        text = f"{article.get('title', '')} {article.get('summary', '')}".upper()
        for symbol in symbols:
            if symbol.upper() in text:
                scores[symbol] = scores.get(symbol, 0.0) + 1.0
    return scores


def _get_top_buy_symbols(settings, news_client: NewsClient, client: BinanceClient, portfolio_manager=None) -> List[Dict]:
    # Expire autonomous signals older than 30 min with no decision (housekeeping)
    expired = expire_stale_signals(max_age_minutes=30)
    if expired:
        print(f"Expired {expired} stale autonomous signal(s)")

    # First try to get signals from external sources (TradingView webhook)
    # Filter out autonomous signals to avoid using stale autonomous signals
    all_signals = get_signals(include_processed=False)
    external_signals = [s for s in all_signals if s.get('source') != 'autonomous']

    # ── Mejora D: TTL filter — discard external signals older than 15 min ─────────
    fresh_external = []
    for sig in external_signals:
        received_raw = sig.get('received_at')
        if received_raw:
            try:
                received_dt = datetime.fromisoformat(received_raw.replace('Z', '+00:00'))
                age_min = (datetime.now(timezone.utc) - received_dt).total_seconds() / 60
                if age_min > 15:
                    print(f"Discarding stale external signal for {sig.get('symbol')} (age={age_min:.1f}min > 15min)")
                    mark_signal_as_processed(sig.get('symbol'))
                    continue
            except Exception:
                pass
        fresh_external.append(sig)
    external_signals = fresh_external

    # If no external signals, use autonomous signal generation
    if not external_signals:
        print("No external signals found, using autonomous signal generation...")
        try:
            # Configure symbols for analysis
            symbols = getattr(settings, 'analysis_symbols', [
                'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
                'ADAUSDT', 'DOGEUSDT', 'AVAXUSDT', 'DOTUSDT', 'LINKUSDT'
            ])
            
            signal_generator = AutonomousSignalGenerator(client, symbols)  # reuse shared client
            autonomous_signals = signal_generator.get_top_buy_signals(
                interval='1h',
                min_strength=getattr(settings, 'min_signal_strength', 0.5),
                limit=getattr(settings, 'max_positions', 5)
            )
            
            # Convert autonomous signals to expected format and add to signal store
            top_signals = []
            for signal in autonomous_signals:
                signal_dict = {
                    "symbol": signal['symbol'],
                    "score": signal['strength'] * 100,  # Convert to 0-100 scale
                    "reason": signal['reason'],
                    "indicators": signal.get('indicators', {}),
                    "strength": signal['strength'],
                    "volume_confirmed": signal.get('volume_confirmed', False),
                    "higher_tf_trend": signal.get('higher_tf_trend', 'FLAT'),
                }
                top_signals.append(signal_dict)
                
                # Add autonomous signal to signal store for analysis cycle
                try:
                    add_signal({
                        "symbol": signal['symbol'],
                        "action": "BUY",
                        "price": signal.get('current_price', 0),
                        "strength": signal['strength'],
                        "reason": signal['reason'],
                        "source": "autonomous"
                    })
                    print(f"Added autonomous signal for {signal['symbol']} to signal store")
                except Exception as e:
                    print(f"Failed to add signal to store: {e}")
            
            print(f"Generated {len(top_signals)} autonomous buy signals")
            return top_signals
            
        except Exception as e:
            print(f"Error in autonomous signal generation: {e}")
            return []
    
    # Use external signals if available
    symbols = sorted({normalize_symbol(signal.get("symbol", "")) for signal in external_signals if signal.get("symbol")})
    news_scores = _get_news_scores(symbols, news_client, settings)

    # ── Mejora C: Enrich external signals with 4h trend + volume + strength ──
    try:
        sig_gen_ext = AutonomousSignalGenerator(client, symbols)
        for sig in external_signals:
            sym = normalize_symbol(sig.get("symbol", ""))
            if not sym:
                continue
            analysis = sig_gen_ext.analyze_symbol(sym, interval='1h', use_multi_timeframe=True)
            sig['strength'] = analysis.get('strength', 0.5)
            sig['higher_tf_trend'] = analysis.get('higher_tf_trend', 'FLAT')
            sig['volume_confirmed'] = analysis.get('volume_confirmed', False)
            if analysis.get('higher_tf_trend') == 'DOWN' and sig.get('action', '').upper() == 'BUY':
                print(f"External signal {sym}: 4h trend DOWN — strength halved to {sig['strength']:.2f}")
    except Exception as ext_err:
        print(f"External signal enrichment error: {ext_err}")
    
    # Get technical scores from analysis cache
    technical_scores: Dict[str, float] = {}
    for symbol in symbols:
        cached = analysis_cache.get(symbol)
        if cached:
            technical_scores[symbol] = cached.get("technical_score", 50.0)
        else:
            technical_scores[symbol] = 50.0  # Default neutral score if no analysis yet
    
    # Use Portfolio Manager if available
    if portfolio_manager:
        # Get risk metrics for all symbols
        from risk_calculator import RiskCalculator
        risk_calc = RiskCalculator()
        risk_metrics = {}
        
        for symbol in symbols:
            # For now, use default risk metrics
            # In production, this would use historical price data
            risk_metrics[symbol] = {"risk_score": 50.0}  # Default medium risk
        
        # Use Portfolio Manager ranking
        signal_scores = rank_for_portfolio_manager(
            external_signals, news_scores, technical_scores, risk_metrics,
            min_confidence=settings.min_pair_confidence_score
        )
        
        # Convert to list of dicts sorted by score
        top_signals = [
            {"symbol": symbol, "score": score}
            for symbol, score in sorted(signal_scores.items(), key=lambda x: x[1], reverse=True)
        ][:settings.max_positions]
    else:
        # Use existing ranking logic
        if any(v != 50.0 for v in technical_scores.values()):
            top_signals = rank_with_technical(external_signals, news_scores, technical_scores, settings.max_positions)
        else:
            top_signals = rank_buy_signals(external_signals, news_scores, settings.max_positions)
    
    return top_signals


def _check_sl_tp_positions(client: BinanceClient, settings, position_manager) -> List[Dict]:
    """
    Check all open positions against Stop-Loss, Take-Profit, and Trailing Stop rules.
    Sells any position that has breached its exit threshold.
    Returns list of closed position dicts.
    """
    closed = []
    if not position_manager:
        return closed

    sl_pct = getattr(settings, 'stop_loss_pct', 1.5) / 100.0
    tp_pct = getattr(settings, 'take_profit_pct', 2.5) / 100.0
    trailing_enabled = getattr(settings, 'trailing_stop_enabled', True)
    trail_activate = getattr(settings, 'trailing_stop_activation_pct', 2.0) / 100.0
    trail_dist = getattr(settings, 'trailing_stop_distance_pct', 1.0) / 100.0
    commission_rate = getattr(settings, 'binance_commission_rate', 0.001)
    if getattr(settings, 'use_bnb_for_commission', False):
        commission_rate = 0.0005

    try:
        all_positions = position_manager.get_all_positions()
    except Exception as e:
        print(f"SL/TP check: cannot get positions: {e}")
        return closed

    for symbol, pos_list in all_positions.items():
        for pos in pos_list:
            try:
                current_price = client.get_symbol_price(symbol)
                entry_price = float(pos.entry_price)
                if entry_price <= 0:
                    continue

                pnl_pct = (current_price - entry_price) / entry_price
                close_reason = None

                # ── Stop-Loss ──────────────────────────────────────────────
                if pnl_pct <= -sl_pct:
                    close_reason = f"stop_loss ({pnl_pct*100:.2f}%)"

                # ── Take-Profit ────────────────────────────────────────────
                elif pnl_pct >= tp_pct:
                    close_reason = f"take_profit ({pnl_pct*100:.2f}%)"

                # ── Trailing Stop ──────────────────────────────────────────
                elif trailing_enabled and pnl_pct >= trail_activate:
                    # Peak is stored in position metadata; update if new high
                    metadata = getattr(pos, 'metadata', {}) or {}
                    peak_price = float(metadata.get('trailing_peak', entry_price))
                    if current_price > peak_price:
                        peak_price = current_price
                        metadata['trailing_peak'] = peak_price
                        try:
                            position_manager.update_position_metadata(pos.position_id, metadata)
                        except Exception:
                            pass
                    trail_stop_price = peak_price * (1.0 - trail_dist)
                    if current_price <= trail_stop_price:
                        close_reason = f"trailing_stop (peak={peak_price:.4f}, trail={trail_stop_price:.4f})"

                if close_reason:
                    try:
                        filters = get_symbol_filters(client, symbol)
                        qty = align_quantity_to_step(float(pos.quantity), filters["step"])
                        valid, reason = is_quantity_valid(qty, current_price, filters)
                        if not valid:
                            print(f"SL/TP: cannot sell {symbol}: {reason}")
                            continue
                        order = client.create_market_order(symbol=symbol, side="SELL", quantity=qty)
                        print(f"SL/TP EXIT {symbol}: {close_reason} @ {current_price:.4f}")
                        ec.add_error(ec.SOURCE_TRADING_CYCLE, ec.LEVEL_INFO,
                                     f"SL/TP exit {symbol}: {close_reason} @ {current_price:.4f}")
                        position_manager.close_all_positions_for_symbol(
                            symbol, current_price, close_reason.split(" ")[0], commission_rate
                        )
                        add_signal({
                            "symbol": symbol, "action": "SELL",
                            "price": current_price,
                            "reason": close_reason,
                            "pnl_pct": round(pnl_pct * 100, 4),
                            "source": close_reason.split("_")[0],
                        })
                        closed.append({"symbol": symbol, "reason": close_reason,
                                       "price": current_price, "order": order})
                    except Exception as exc:
                        print(f"SL/TP sell failed for {symbol}: {exc}")
                        ec.add_error(ec.SOURCE_BINANCE, ec.LEVEL_ERROR,
                                     f"SL/TP sell failed {symbol}: {exc}")
            except Exception as e:
                print(f"SL/TP check error for {symbol}: {e}")
    return closed


def _sell_holdings_not_in_top(client: BinanceClient, settings, balances: Dict[str, float], top_symbols: List[str], portfolio_manager=None, position_manager=None) -> List[Dict]:
    orders = []
    for asset, amount in balances.items():
        if asset.upper() == "USDT" or amount <= 0:
            continue

        symbol = f"{asset.upper()}USDT"
        if symbol in _INVALID_SYMBOLS:
            continue
        if symbol in top_symbols:
            print(f"Keeping asset {asset} because {symbol} is in top symbols")
            continue

        # Protect profitable positions from forced rebalancing
        if getattr(settings, 'protect_profitable_positions', True) and position_manager:
            try:
                pos_list = position_manager.get_all_positions().get(symbol, [])
                if pos_list:
                    current_price = client.get_symbol_price(symbol)
                    min_pct = getattr(settings, 'protect_profit_min_pct', 1.0) / 100.0
                    is_profitable = any(
                        (current_price - float(p.entry_price)) / float(p.entry_price) >= min_pct
                        for p in pos_list if float(p.entry_price) > 0
                    )
                    if is_profitable:
                        print(f"Protecting profitable position {symbol} from rebalancing")
                        continue
            except Exception:
                pass

        try:
            filters = get_symbol_filters(client, symbol)
            price = client.get_symbol_price(symbol)
        except Exception as exc:
            if "-1121" in str(exc):
                _INVALID_SYMBOLS.add(symbol)
            else:
                print(f"Skip sell {symbol}: {exc}")
            continue

        qty = align_quantity_to_step(amount, filters["step"])
        valid, reason = is_quantity_valid(qty, price, filters)
        if not valid:
            print(f"Skipping sell {symbol}: {reason}")
            continue

        try:
            order = client.create_market_order(symbol=symbol, side="SELL", quantity=qty)
            orders.append({"symbol": symbol, "side": "SELL", "quantity": qty, "order": order})
            print(f"Sold {qty} {symbol}")
            add_signal({
                "symbol": symbol, "action": "SELL",
                "price": price,
                "reason": "portfolio_rebalancing",
                "source": "rebalancing",
            })

            # Close position in Position Manager if available
            if position_manager:
                try:
                    commission_rate = getattr(settings, 'binance_commission_rate', 0.001)
                    if getattr(settings, 'use_bnb_for_commission', False):
                        commission_rate = 0.0005
                    position_manager.close_all_positions_for_symbol(symbol, price, "portfolio_rebalancing", commission_rate)
                except Exception as e:
                    print(f"Failed to close position in Position Manager: {e}")
                    ec.add_error(ec.SOURCE_POSITION_MANAGER, ec.LEVEL_WARNING, f"Failed to close position {symbol}: {e}")
        except Exception as exc:
            print(f"Sell order failed for {symbol}: {exc}")
            ec.add_error(ec.SOURCE_BINANCE, ec.LEVEL_ERROR, f"Sell order failed for {symbol}: {exc}")
    return orders


def _kelly_position_size(settings, strength: float, prediction: dict,
                         usdt_liquid: float, sl_pct: float = 0.015,
                         tp_pct: float = 0.025) -> float:
    """
    Half-Kelly position sizing scaled by signal quality.

    Tiers (prob / strength thresholds):
      FUERTE  prob >= 0.65 + strength >= 0.80  → kelly_max_usdt  ($150)
      ALTA    prob >= 0.60 + strength >= 0.70  → 80% of max       ($120)
      MEDIA   prob >= 0.55 + strength >= 0.55  → 60% of max       ($90)
      BASE    prob >= 0.50  (passed validation) → kelly_base_usdt  ($60)

    Fallback (kelly disabled): max_per_trade_usdt.
    Hard cap: kelly_max_pct_liquid of available USDT.
    """
    if not getattr(settings, "kelly_enabled", True):
        return getattr(settings, "max_per_trade_usdt", 50.0)

    base = getattr(settings, "kelly_base_usdt", 60.0)
    max_usdt = getattr(settings, "kelly_max_usdt", 150.0)
    cap_pct = getattr(settings, "kelly_max_pct_liquid", 0.40)

    prob = float((prediction or {}).get("success_probability", 0.50))
    r = tp_pct / sl_pct if sl_pct > 0 else 1.67

    # Half-Kelly fraction
    kelly_f = max(0.0, (prob - (1 - prob) / r)) * 0.5

    # Map to tier
    if prob >= 0.65 and strength >= 0.80:
        tier, amount = "FUERTE", max_usdt
    elif prob >= 0.60 and strength >= 0.70:
        tier, amount = "ALTA", base + (max_usdt - base) * 0.80
    elif prob >= 0.55 and strength >= 0.55:
        tier, amount = "MEDIA", base + (max_usdt - base) * 0.50
    else:
        tier, amount = "BASE", base

    # Scale further by half-kelly fraction (softens high-tier amounts when kelly_f is low)
    scaled = amount * min(1.0, kelly_f * 10)
    # Never go below base
    sized = max(base, scaled)

    # Hard cap: fraction of liquid USDT
    liquid_cap = usdt_liquid * cap_pct
    final = min(sized, liquid_cap, max_usdt)

    print(f"Kelly sizing: prob={prob:.2f}, strength={strength:.2f}, kelly_f={kelly_f:.3f} "
          f"→ tier={tier}, sized=${sized:.2f}, cap=${liquid_cap:.2f} → final=${final:.2f} USDT")
    return final


def _buy_top_symbols(client: BinanceClient, settings, top_signals_with_meta: List, portfolio_manager=None, position_manager=None, precomputed_predictions: Dict = None) -> List[Dict]:
    # Accept either plain symbol strings or dicts with metadata (for aggressive mode)
    if top_signals_with_meta and isinstance(top_signals_with_meta[0], dict):
        top_symbols = [s['symbol'] for s in top_signals_with_meta]
        signal_meta = {s['symbol']: s for s in top_signals_with_meta}
    else:
        top_symbols = list(top_signals_with_meta)
        signal_meta = {}

    orders = []
    if not top_symbols:
        return orders

    # ── BTC macro trend filter ─────────────────────────────────────────────────
    btc_trend = 'FLAT'
    try:
        sig_gen = AutonomousSignalGenerator(client, ['BTCUSDT'])
        btc_trend = sig_gen.get_btc_market_trend(interval='4h')
        print(f"BTC 4h trend: {btc_trend}")
    except Exception as e:
        print(f"BTC trend check failed: {e}")

    balances = client.get_account_balance()
    usdt = float(balances.get("USDT", 0.0))
    usdt_liquid = usdt  # default; overridden below if portfolio_manager available
    
    # Use Portfolio Manager for allocation if available
    if portfolio_manager:
        balance = portfolio_manager.get_total_balance()
        # Use liquid USDT as the actual cap — not total portfolio value
        usdt_liquid = float(balance.get("usdt_available", usdt))
        allocation = min(settings.max_per_trade_usdt, usdt_liquid / max(len(top_symbols), 1))
        print(f"USDT liquid: {usdt_liquid}, total portfolio: {balance['total_usdt']:.2f}, allocation per symbol: {allocation:.4f}")
    else:
        if usdt <= 0:
            print("No USDT available for buys")
            return orders
        allocation = min(settings.max_per_trade_usdt, usdt / len(top_symbols))
        print(f"USDT available: {usdt}, allocation per symbol: {allocation}")

    # ── BTC bearish market: reduce allocation 50% ──────────────────────────────
    if btc_trend == 'DOWN':
        allocation *= 0.5
        print(f"BTC bearish — allocation reduced 50% to {allocation:.4f} USDT per symbol")
    
    # If USDT is too low, try to sell non-top assets first to get more USDT
    if usdt < allocation * len(top_symbols) and portfolio_manager:
        print(f"USDT available ({usdt}) is less than needed ({allocation * len(top_symbols)}), selling non-top assets...")
        sell_orders = _sell_holdings_not_in_top(client, settings, balances, top_symbols, portfolio_manager, position_manager)
        orders.extend(sell_orders)
        
        # Refresh USDT balance after selling
        balances = client.get_account_balance()
        usdt = float(balances.get("USDT", 0.0))
        print(f"USDT after selling non-top assets: {usdt}")

    for symbol in top_symbols:
        # Enforce global MAX_POSITIONS limit
        if position_manager:
            total_open = sum(len(v) for v in position_manager.get_all_positions().values())
            if total_open >= settings.max_positions:
                print(f"Skipping buy {symbol}: global max positions ({total_open}/{settings.max_positions}) reached")
                update_signal_decision(symbol, "rejected", "MAX_POSIC")
                break
        # Enforce per-symbol MAX_POSITIONS_PER_SYMBOL limit
        if position_manager:
            count = position_manager.get_positions_count(symbol)
            max_per = getattr(settings, 'max_positions_per_symbol', 3)
            if count >= max_per:
                print(f"Skipping buy {symbol}: per-symbol limit ({count}/{max_per}) reached")
                update_signal_decision(symbol, "rejected", "LÍM_SÍMBOLO")
                continue

        try:
            price = client.get_symbol_price(symbol)
            filters = get_symbol_filters(client, symbol)
        except Exception as exc:
            print(f"Skip buy {symbol}: {exc}")
            update_signal_decision(symbol, "rejected", "ERROR_PRECIO")
            continue

        # ── Signal metadata ───────────────────────────────────────────────
        meta = signal_meta.get(symbol, {})
        strength = float(meta.get('strength', 0.0))

        # ── PnL prediction (needed for Kelly sizing) ───────────────────────
        if precomputed_predictions and symbol in precomputed_predictions:
            prediction = precomputed_predictions[symbol]
            formatted_indicators = None
        else:
            prediction, formatted_indicators = pnl_service.predict_for_symbol(
                symbol, client, settings, allocation
            )
        is_valid, reason = pnl_service.validate(prediction, settings)
        if not is_valid:
            ep = prediction.get('expected_pnl', 0) if prediction else 0
            sp = prediction.get('success_probability', 0) if prediction else 0
            # ── VolSpike override for narrative tokens ─────────────────────
            volspike_override = False
            if getattr(settings, 'volspike_override_enabled', True):
                narrative_set = set(getattr(settings, 'narrative_tokens', []))
                only_narrative = getattr(settings, 'volspike_override_narrative_only', True)
                if not only_narrative or symbol in narrative_set:
                    try:
                        _kl = client.client.get_klines(symbol=symbol, interval='4h', limit=26)
                        _closes = [float(k[4]) for k in _kl]
                        _vols   = [float(k[5]) for k in _kl]
                        _avg_vol = sum(_vols[:-1]) / max(len(_vols) - 1, 1)
                        _spike   = _vols[-1] / _avg_vol if _avg_vol > 0 else 0
                        # RSI 14 on 4h closes
                        _gains = _losses = 0.0
                        for _i in range(1, 15):
                            _d = _closes[_i] - _closes[_i - 1]
                            if _d > 0: _gains += _d
                            else: _losses += -_d
                        _ag = _gains / 14; _al = _losses / 14
                        _rsi = 100 - 100 / (1 + _ag / _al) if _al > 0 else 100
                        # EMA9 vs EMA21 for 4h trend
                        _k9 = 2/10; _k21 = 2/22; _e9 = _closes[0]; _e21 = _closes[0]
                        for _c in _closes[1:]: _e9 = _c*_k9 + _e9*(1-_k9); _e21 = _c*_k21 + _e21*(1-_k21)
                        _trend_up = _e9 > _e21
                        _min_spike = getattr(settings, 'volspike_override_min_spike', 1.5)
                        _max_rsi   = getattr(settings, 'volspike_override_max_rsi', 65.0)
                        _need_trend = getattr(settings, 'volspike_override_trend_required', True)
                        if (_spike >= _min_spike and _rsi < _max_rsi
                                and (not _need_trend or _trend_up)):
                            volspike_override = True
                            print(f"VolSpike OVERRIDE {symbol}: spike={_spike:.2f}x, RSI4h={_rsi:.1f}, trend={'UP' if _trend_up else 'DOWN'} → bypassing PnL veto (pred={ep:.2f}, prob={sp:.2f})")
                    except Exception as _ve:
                        print(f"VolSpike override check failed for {symbol}: {_ve}")
            if not volspike_override:
                print(f"Skipping buy {symbol}: {reason} (pred={ep:.2f} USDT, prob={sp:.2f})")
                update_signal_decision(symbol, "rejected", "PNL_BAJO")
                continue
        if prediction and is_valid:
            print(f"Prediction OK for {symbol}: {prediction['expected_pnl']:.2f} USDT, prob={prediction['success_probability']:.2f}")

        # ── Kelly dynamic position sizing ───────────────────────────────
        sl_pct = getattr(settings, 'stop_loss_pct', 1.5) / 100.0
        tp_pct = getattr(settings, 'take_profit_pct', 2.5) / 100.0
        usdt_liquid_now = float(client.get_account_balance().get("USDT", usdt_liquid))
        trade_allocation = _kelly_position_size(
            settings, strength, prediction, usdt_liquid_now, sl_pct, tp_pct
        )

        qty = align_quantity_to_step(trade_allocation / price, filters["step"])
        valid, reason = is_quantity_valid(qty, price, filters)
        if not valid:
            print(f"Skipping buy {symbol}: {reason}")
            update_signal_decision(symbol, "rejected", "QTY_INVÁLIDA")
            continue

        # Guard: verify liquid USDT is sufficient before placing order
        cost_estimate = qty * price * 1.002  # include ~0.2% buffer for fees
        if usdt_liquid_now < cost_estimate:
            print(f"Skipping buy {symbol}: insufficient USDT ({usdt_liquid_now:.4f} < {cost_estimate:.4f} needed)")
            ec.add_error(ec.SOURCE_TRADING_CYCLE, ec.LEVEL_WARNING,
                         f"Skipped buy {symbol}: insufficient USDT ({usdt_liquid_now:.4f} < {cost_estimate:.4f})")
            update_signal_decision(symbol, "rejected", "SIN_SALDO")
            continue

        try:
            order = client.create_market_order(symbol=symbol, side="BUY", quantity=qty)
            orders.append({"symbol": symbol, "side": "BUY", "quantity": qty, "order": order})
            print(f"Bought {qty} {symbol}")
            ec.add_error(ec.SOURCE_BINANCE, ec.LEVEL_INFO, f"Bought {qty} {symbol} @ ~{price:.4f} USDT")
            update_signal_decision(symbol, "executed", "EJECUTADO")
            mark_signal_as_processed(symbol)

            # Track position in Position Manager if available
            if position_manager:
                try:
                    from position_manager import Position
                    from datetime import datetime
                    from technical_indicators import TechnicalIndicators
                    entry_commission = compute_commission(qty, price, settings)

                    # ── ATR-based dynamic SL/TP ──────────────────────────────────
                    sl_price = tp_price = None
                    try:
                        sig_gen_atr = AutonomousSignalGenerator(client, [symbol])
                        klines_atr = sig_gen_atr.fetch_klines(symbol, '15m', limit=50)
                        if klines_atr and len(klines_atr) >= 15:
                            highs_a  = [k['high']  for k in klines_atr]
                            lows_a   = [k['low']   for k in klines_atr]
                            closes_a = [k['close'] for k in klines_atr]
                            atr_vals = TechnicalIndicators.atr(highs_a, lows_a, closes_a, 14)
                            if atr_vals:
                                atr = atr_vals[-1]
                                sl_price = price - atr * 1.5
                                tp_price = price + atr * 2.5
                                print(f"ATR-based SL/TP for {symbol}: ATR={atr:.5f} SL={sl_price:.5f} TP={tp_price:.5f}")
                    except Exception as atr_err:
                        print(f"ATR calc failed for {symbol}, using pct fallback: {atr_err}")

                    # Fallback to percentage-based if ATR unavailable
                    if sl_price is None:
                        sl_pct = getattr(settings, 'stop_loss_pct', 1.5) / 100.0
                        tp_pct = getattr(settings, 'take_profit_pct', 2.5) / 100.0
                        sl_price = price * (1 - sl_pct)
                        tp_price = price * (1 + tp_pct)

                    # Prepare metadata with prediction
                    metadata = {"order_id": order.get("orderId")}
                    if prediction:
                        metadata['pnl_prediction'] = prediction
                        metadata['indicators_at_entry'] = formatted_indicators

                    position = Position(
                        symbol=symbol,
                        side="BUY",
                        quantity=qty,
                        entry_price=price,
                        entry_time=datetime.utcnow().isoformat(),
                        stop_loss=sl_price,
                        take_profit=tp_price,
                        metadata=metadata,
                        entry_commission=entry_commission,
                        pnl_prediction=prediction,
                        indicators_at_entry=formatted_indicators
                    )
                    position_manager.add_position(position)
                    print(f"Position tracked with entry commission: {entry_commission:.4f} USDT")
                    if prediction:
                        print(f"Position includes PnL prediction: {prediction['expected_pnl']:.2f} USDT")
                except Exception as e:
                    print(f"Failed to track position in Position Manager: {e}")
                    ec.add_error(ec.SOURCE_POSITION_MANAGER, ec.LEVEL_WARNING, f"Failed to track position {symbol}: {e}")
        except Exception as exc:
            print(f"Buy order failed for {symbol}: {exc}")
            ec.add_error(ec.SOURCE_BINANCE, ec.LEVEL_ERROR, f"Buy order failed for {symbol}: {exc}")
            update_signal_decision(symbol, "rejected", "ERROR_ORDEN")
    return orders


def _select_symbols(settings, news_client: NewsClient, client: BinanceClient, portfolio_manager) -> List[Dict]:
    """Step 1: Resolve top buy symbols (as signal dicts) from signals or autonomous generation."""
    top_signals = _get_top_buy_symbols(settings, news_client, client, portfolio_manager)
    symbols = [s["symbol"] for s in top_signals]
    print("Top buy symbols:", symbols)
    return top_signals


def _build_predictions(top_symbols: List[str], client: BinanceClient, settings, portfolio_manager) -> Dict:
    """Step 2: Generate PnL predictions once for all top symbols (reused in buy step)."""
    predictions = {}
    if not portfolio_manager or not top_symbols:
        return predictions
    try:
        usdt_liquid = float(portfolio_manager.get_total_balance().get("usdt_available", 0))
        alloc_per = min(settings.max_per_trade_usdt, usdt_liquid / len(top_symbols))
        for symbol in top_symbols:
            pred, _ = pnl_service.predict_for_symbol(symbol, client, settings, alloc_per)
            if pred:
                predictions[symbol] = pred
                print(f"Prediction for {symbol}: {pred['expected_pnl']:.2f} USDT")
    except Exception as e:
        print(f"Error generating predictions: {e}")
    return predictions


def _close_prediction_based(top_symbols: List[str], client: BinanceClient, settings,
                            portfolio_manager, position_manager, predictions: Dict) -> None:
    """Step 3: Close positions whose predictions suggest switching to better symbols."""
    if not portfolio_manager or not position_manager or not predictions:
        return
    try:
        current_positions = {}
        for sym, pos_list in position_manager.get_all_positions().items():
            if pos_list:
                pos = pos_list[0]
                current_positions[sym] = {
                    'quantity': float(pos.quantity),
                    'entry_price': float(pos.entry_price),
                }
        if not current_positions:
            return
        print(f"Current open positions: {list(current_positions.keys())}")

        from portfolio_manager import CapitalAllocation
        usdt_liquid = float(portfolio_manager.get_total_balance().get("usdt_available", 0))
        alloc_per = min(settings.max_per_trade_usdt, usdt_liquid / len(top_symbols))
        new_allocations = [
            CapitalAllocation(
                symbol=sym,
                capital_usdt=alloc_per,
                percentage=alloc_per / usdt_liquid * 100 if usdt_liquid else 0,
                risk_adjusted=False,
            )
            for sym in top_symbols
        ]
        symbols_to_close = portfolio_manager.decide_position_closures(
            current_positions=current_positions,
            new_allocations=new_allocations,
            new_predictions=predictions,
        )
        if symbols_to_close:
            print(f"Closing positions based on predictions: {symbols_to_close}")
            time.sleep(2)
    except Exception as e:
        print(f"Error in prediction-based position closure: {e}")


def execute_once():
    from init_components import get_components
    comp = get_components()
    settings = comp.settings
    client = comp.binance_client
    news_client = comp.news_client
    position_manager = comp.position_manager
    portfolio_manager = comp.portfolio_manager

    print(f"DRY_RUN={settings.dry_run}  ALLOW_REAL_TRADING={settings.allow_real_trading}  MAX_PER_TRADE_USDT={settings.max_per_trade_usdt}")

    if not settings.binance_use_testnet and not settings.allow_real_trading:
        print("Real trading is disabled. Skipping execute_once.")
        return {"top_symbols": [], "sell_orders": [], "buy_orders": []}

    if settings.agent_mode != "auto":
        print("Agent mode is not auto. Skipping execute_once.")
        return {"top_symbols": [], "sell_orders": [], "buy_orders": []}

    # ── Step 0: SL/TP monitor — runs every cycle before any new trades ─────────
    sl_tp_closes = _check_sl_tp_positions(client, settings, position_manager)
    if sl_tp_closes:
        print(f"SL/TP closed {len(sl_tp_closes)} position(s): {[c['symbol'] for c in sl_tp_closes]}")

    # ── Step 1: Determine top symbols ─────────────────────────────────────────
    top_signals = _select_symbols(settings, news_client, client, portfolio_manager)
    if not top_signals:
        print("No buy signals available; skipping new buys this cycle.")
        return {"top_symbols": [], "sell_orders": [], "buy_orders": [], "sl_tp_closes": sl_tp_closes}

    # Normalize: _select_symbols may return dicts or strings
    if top_signals and isinstance(top_signals[0], dict):
        top_symbols = [s['symbol'] for s in top_signals]
    else:
        top_symbols = list(top_signals)

    # ── Step 2: Generate predictions once (reused by step 3 and step 5) ───────
    predictions = _build_predictions(top_symbols, client, settings, portfolio_manager)

    # ── Step 3: Prediction-based position closure ──────────────────────────────
    _close_prediction_based(top_symbols, client, settings, portfolio_manager, position_manager, predictions)

    # ── Step 4: Sell holdings no longer in top ─────────────────────────────────
    balances = client.get_account_balance()
    print("Balances:", balances)
    sell_orders = _sell_holdings_not_in_top(client, settings, balances, top_symbols, portfolio_manager, position_manager)
    if sell_orders:
        balances = client.get_account_balance()
        print("Balances after sells:", balances)

    # ── Step 5: Buy top symbols — pass full signal dicts for aggressive mode ───
    buy_orders = _buy_top_symbols(client, settings, top_signals, portfolio_manager, position_manager, precomputed_predictions=predictions)

    print("Completed cycle.")
    return {"top_symbols": top_symbols, "sell_orders": sell_orders,
            "buy_orders": buy_orders, "sl_tp_closes": sl_tp_closes}


if __name__ == '__main__':
    settings = get_settings()
    execute_once()

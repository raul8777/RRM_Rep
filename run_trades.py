import time
from datetime import datetime, timezone
from typing import Dict, List

import error_collector as ec
from binance_client import BinanceClient
from market_regime import get_regime, MarketRegime
from futures_client import FuturesClient
from news_client import NewsClient
from signal_store import get_signals, add_signal, mark_signal_as_processed, update_signal_decision, expire_stale_signals
from strategy import rank_buy_signals, rank_with_technical, normalize_symbol, rank_for_portfolio_manager
from analysis_cache import analysis_cache
from autonomous_signals import AutonomousSignalGenerator
from trade_utils import align_quantity_to_step, get_symbol_filters, is_quantity_valid, compute_commission
import pnl_service


# Symbols confirmed invalid on Binance (delisted) — skip without API call
_INVALID_SYMBOLS: set = {"EONUSDT", "ADDUSDT", "MEETONEUSDT", "ATDUSDT", "EOPUSDT", "ETHWUSDT"}

# VolSpike override cooldown: {symbol: datetime_of_last_override_entry}
# Prevents re-entering the same narrative token breakout every 5min cycle
_VOLSPIKE_COOLDOWN: Dict[str, datetime] = {}

# Futures client singleton (lazy-initialized)
_futures_client: FuturesClient = None


def _get_futures_client(settings) -> FuturesClient:
    """Lazy singleton for FuturesClient. Reuses instance across cycles."""
    global _futures_client
    if _futures_client is None:
        _futures_client = FuturesClient(
            api_key    = settings.binance_api_key,
            api_secret = settings.binance_api_secret,
            dry_run    = settings.dry_run,
        )
        print("[Futures] FuturesClient initialized")
    return _futures_client


_VOLSPIKE_COOLDOWN_HOURS = 4.0

# NARRATIVE token buy cooldown: prevent buying the same symbol multiple times
# within a single block of cycles (prevents 3 FET entries in 10 minutes)
_NARRATIVE_BUY_COOLDOWN: Dict[str, datetime] = {}
_NARRATIVE_BUY_COOLDOWN_HOURS = 4.0


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


def _get_top_buy_symbols(settings, news_client: NewsClient, client: BinanceClient, portfolio_manager=None, regime: str = "SPOT_BULL") -> List[Dict]:
    # Expire autonomous signals older than 10 min with no decision (B3: matches SIGNAL_CACHE_TTL)
    expired = expire_stale_signals(max_age_minutes=10)
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
                limit=getattr(settings, 'max_positions', 5),
                news_client=news_client
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
            analysis = sig_gen_ext.analyze_symbol(sym, interval='1h', use_multi_timeframe=True, news_client=news_client)
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
            min_confidence=settings.min_pair_confidence_score, regime=regime
        )
        
        # Convert to list of dicts sorted by score
        top_signals = [
            {"symbol": symbol, "score": score}
            for symbol, score in sorted(signal_scores.items(), key=lambda x: x[1], reverse=True)
        ][:settings.max_positions]
    else:
        # Use existing ranking logic
        if any(v != 50.0 for v in technical_scores.values()):
            top_signals = rank_with_technical(external_signals, news_scores, technical_scores, settings.max_positions, regime=regime)
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

    _global_sl_pct = getattr(settings, 'stop_loss_pct', 1.5)
    _global_tp_pct = getattr(settings, 'take_profit_pct', 2.5)
    trailing_enabled = getattr(settings, 'trailing_stop_enabled', True)
    _global_trail_act = getattr(settings, 'trailing_stop_activation_pct', 2.0)
    _global_trail_dist = getattr(settings, 'trailing_stop_distance_pct', 1.0)
    commission_rate = getattr(settings, 'binance_commission_rate', 0.001)
    if getattr(settings, 'use_bnb_for_commission', False):
        commission_rate = 0.0005
    _token_categories = getattr(settings, 'token_categories', {})
    _cat_thresholds = getattr(settings, 'category_thresholds', {})

    try:
        all_positions = position_manager.get_all_positions()
    except Exception as e:
        print(f"SL/TP check: cannot get positions: {e}")
        return closed

    for symbol, pos_list in all_positions.items():
        # ── Per-category SL/TP/trailing thresholds ────────────────────────
        _cat_beh = _token_categories.get(symbol, {}).get('market_behavior', 'MIDCAP_ALT')
        _cat_th = _cat_thresholds.get(_cat_beh, {})
        sl_pct = _cat_th.get('stop_loss_pct', _global_sl_pct) / 100.0
        tp_pct = _cat_th.get('take_profit_pct', _global_tp_pct) / 100.0
        trail_activate = _cat_th.get('trailing_activation_pct', _global_trail_act) / 100.0
        trail_dist = _cat_th.get('trailing_distance_pct', _global_trail_dist) / 100.0

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
                        full_qty = float(pos.quantity)
                        _scale_out = getattr(settings, 'scale_out_enabled', True)
                        _scale_pct = getattr(settings, 'scale_out_first_close_pct', 0.5)
                        _is_tp = close_reason.startswith("take_profit")

                        if _scale_out and _is_tp and full_qty * _scale_pct >= filters.get("min_qty", 0):
                            # ── Scale-out: sell 50% at TP, move SL to breakeven ──────────
                            sell_qty = align_quantity_to_step(full_qty * _scale_pct, filters["step"])
                            valid, reason = is_quantity_valid(sell_qty, current_price, filters)
                            if valid:
                                order = client.create_market_order(symbol=symbol, side="SELL", quantity=sell_qty)
                                print(f"SCALE-OUT {symbol}: sold {sell_qty} ({_scale_pct*100:.0f}%) @ {current_price:.4f}, SL→breakeven={entry_price:.4f}")
                                metadata = getattr(pos, 'metadata', {}) or {}
                                metadata['scale_out_done'] = True
                                metadata['trailing_peak'] = current_price
                                try:
                                    position_manager.update_position_metadata(pos.position_id, metadata)
                                    position_manager.update_position_sl(pos.position_id, entry_price)
                                except Exception:
                                    pass
                                ec.add_error(ec.SOURCE_TRADING_CYCLE, ec.LEVEL_INFO,
                                             f"Scale-out {symbol}: {sell_qty} @ {current_price:.4f}")
                                add_signal({"symbol": symbol, "action": "SELL", "price": current_price,
                                            "reason": f"scale_out_tp ({pnl_pct*100:.2f}%)",
                                            "pnl_pct": round(pnl_pct * 100, 4), "source": "scale_out"})
                                closed.append({"symbol": symbol, "reason": f"scale_out_tp",
                                               "price": current_price, "order": order})
                                continue
                        qty = align_quantity_to_step(full_qty, filters["step"])
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

    fixed_base = getattr(settings, "kelly_base_usdt", 60.0)
    dyn_pct = getattr(settings, "kelly_dynamic_base_pct", 0.15)
    base = max(fixed_base, usdt_liquid * dyn_pct)
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


def _buy_top_symbols(client: BinanceClient, settings, top_signals_with_meta: List, portfolio_manager=None, position_manager=None, precomputed_predictions: Dict = None, news_client=None) -> List[Dict]:
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

    # ── Market Regime Detection — adaptive mode router ─────────────────────────
    _regime: MarketRegime = None
    _regime_mode = "SPOT_BULL"
    if getattr(settings, 'market_regime_enabled', True):
        try:
            _regime = get_regime(client, settings=settings)
            _regime_mode = _regime.mode
            print(f"[Regime] {_regime_mode} | {_regime.regime} | {_regime.reason} | confirmed={_regime.confirmed}")
        except Exception as _re:
            print(f"[Regime] detection failed: {_re}")

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

    # ── Bear mode derived from market_regime (single source of truth) ──────────
    # _bear_mode_active replaced by _regime.mode PRESERVATION/FUTURES_SHORT
    # This eliminates the triple Kelly penalty bug (btc_trend×0.5 + bear×0.5 + regime×0.5 = $7.50)
    _bear_mode_active = (_regime_mode in ("PRESERVATION", "FUTURES_SHORT")) and (_regime is not None and _regime.confirmed)
    if _bear_mode_active:
        print(f"[Regime] Bear mode active via {_regime_mode} (confirmed) — NARRATIVE-only filter enabled")

    # ── BTC dominance filter — skip altcoins if BTC.D too high ────────────────
    _btc_dom_high = False
    if getattr(settings, 'btc_dominance_filter_enabled', True):
        try:
            import urllib.request as _ur, json as _json
            with _ur.urlopen('https://api.coingecko.com/api/v3/global', timeout=3) as _r:
                _gdata = _json.loads(_r.read())
            _btc_dom = _gdata.get('data', {}).get('market_cap_percentage', {}).get('btc', 0)
            _dom_thresh = getattr(settings, 'btc_dominance_altcoin_threshold', 56.0)
            if _btc_dom > _dom_thresh:
                _btc_dom_high = True
                print(f"BTC Dominance {_btc_dom:.1f}% > {_dom_thresh}% — altcoin entries restricted to NARRATIVE only")
        except Exception:
            pass

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

        # ── Bear mode (PRESERVATION/FUTURES_SHORT) — restrict non-NARRATIVE ──
        narrative_set = set(getattr(settings, 'narrative_tokens', []))
        if _bear_mode_active and _regime_mode == "PRESERVATION" and symbol not in narrative_set:
            _bear_min_spike = getattr(settings, 'bear_market_volspike_min', 2.0)
            _meta_spike = float(signal_meta.get(symbol, {}).get('vol_spike', 0))
            if _meta_spike < _bear_min_spike:
                print(f"Skipping buy {symbol}: PRESERVATION mode, not NARRATIVE, vol_spike={_meta_spike:.2f} < {_bear_min_spike}")
                update_signal_decision(symbol, "rejected", "PRESERVATION")
                continue

        # ── FUTURES_SHORT mode — open short via futures instead of spot buy ─
        if _regime and _regime.confirmed and _regime_mode == "FUTURES_SHORT":
            if getattr(settings, 'futures_enabled', False):
                _futures_symbols = list(getattr(settings, 'futures_symbols', ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT']))
                if symbol in _futures_symbols:
                    _fc = _get_futures_client(settings)
                    _f_usdt  = getattr(settings, 'futures_usdt_per_trade', 20.0)
                    _f_sl    = getattr(settings, 'futures_sl_pct', 3.0)
                    _f_tp    = getattr(settings, 'futures_tp_pct', 5.0)
                    _f_lev   = getattr(settings, 'futures_leverage', 2)
                    # Fear & Greed boost: Extreme Fear (<25) → larger short margin (better entry)
                    try:
                        if news_client:
                            _fng = news_client.get_fear_greed_index()
                            _fng_val = _fng.get('value', 50) if isinstance(_fng, dict) else 50
                            if _fng_val < 25:
                                _f_usdt = round(_f_usdt * 1.3, 2)
                                print(f"[Futures] F&G Extreme Fear ({_fng_val}) → boosted margin ${_f_usdt}")
                            elif _fng_val > 75:
                                _f_usdt = round(_f_usdt * 0.8, 2)
                                print(f"[Futures] F&G Extreme Greed ({_fng_val}) → reduced short margin ${_f_usdt}")
                    except Exception:
                        pass
                    print(f"[Futures] FUTURES_SHORT mode: opening short {symbol} margin=${_f_usdt}")
                    _pos = _fc.open_short(symbol, usdt_amount=_f_usdt,
                                          sl_pct=_f_sl, tp_pct=_f_tp, leverage=_f_lev)
                    if _pos:
                        orders.append({"symbol": symbol, "side": "SHORT_FUTURES",
                                        "qty": _pos.qty, "entry_price": _pos.entry_price,
                                        "sl": _pos.sl_price, "tp": _pos.tp_price})
                        update_signal_decision(symbol, "executed", "FUTURES_SHORT")
                    else:
                        update_signal_decision(symbol, "rejected", "FUTURES_BLOCKED")
                    continue  # skip spot buy for this symbol
                else:
                    print(f"[Futures] {symbol} not in futures_symbols — skipping short, no spot entry in FUTURES_SHORT mode")
                    update_signal_decision(symbol, "rejected", "NOT_FUTURES_SYM")
                    continue
            else:
                # futures_enabled=False but mode is FUTURES_SHORT → treat as PRESERVATION
                _regime_mode = "PRESERVATION"

        # ── Market Regime gate — mode-specific entry filters ─────────────
        if _regime and _regime.confirmed:
            if _regime_mode == "PRESERVATION":
                # Only NARRATIVE tokens allowed in preservation mode
                if getattr(settings, 'preservation_narrative_only', True) and symbol not in narrative_set:
                    print(f"Skipping buy {symbol}: PRESERVATION mode — only NARRATIVE allowed")
                    update_signal_decision(symbol, "rejected", "PRESERVATION")
                    continue
                # RSI check for PRESERVATION
                _prsv_max_rsi = getattr(settings, 'preservation_max_rsi', 65.0)
                _meta_rsi = float(signal_meta.get(symbol, {}).get('rsi', 50))
                if _meta_rsi > _prsv_max_rsi:
                    print(f"Skipping buy {symbol}: PRESERVATION mode RSI={_meta_rsi:.1f} > {_prsv_max_rsi}")
                    update_signal_decision(symbol, "rejected", "PRSV_RSI")
                    continue

            elif _regime_mode == "RANGE_SCALP":
                # Only enter when RSI < range_scalp_max_rsi (oversold — near BB lower)
                _rs_max_rsi = getattr(settings, 'range_scalp_max_rsi', 40.0)
                _meta_rsi   = float(signal_meta.get(symbol, {}).get('rsi', 50))
                if _meta_rsi > _rs_max_rsi:
                    print(f"Skipping buy {symbol}: RANGE_SCALP mode RSI={_meta_rsi:.1f} > {_rs_max_rsi} (not at BB lower)")
                    update_signal_decision(symbol, "rejected", "RANGE_RSI")
                    continue
                # B1: Check real BB lower band touch (not just RSI proxy)
                if getattr(settings, 'range_scalp_require_bb_lower', True):
                    _meta_bb_touch = signal_meta.get(symbol, {}).get('bb_lower_touch', None)
                    if _meta_bb_touch is False:  # explicitly False (not None = unknown)
                        print(f"Skipping buy {symbol}: RANGE_SCALP — price not at BB lower band")
                        update_signal_decision(symbol, "rejected", "RANGE_BB")
                        continue

        # ── BTC dominance filter — restrict non-NARRATIVE when BTC.D high ─
        if _btc_dom_high and symbol not in narrative_set:
            print(f"Skipping buy {symbol}: BTC.D high — only NARRATIVE allowed")
            update_signal_decision(symbol, "rejected", "BTC_DOM")
            continue

        # ── NARRATIVE token buy cooldown ─────────────────────────────────
        if symbol in narrative_set:
            from datetime import datetime as _dt2
            _last_buy = _NARRATIVE_BUY_COOLDOWN.get(symbol)
            if _last_buy:
                _h_since = (_dt2.now(timezone.utc) - _last_buy).total_seconds() / 3600
                if _h_since < _NARRATIVE_BUY_COOLDOWN_HOURS:
                    print(f"Skipping buy {symbol}: NARRATIVE cooldown active ({_h_since:.1f}h < {_NARRATIVE_BUY_COOLDOWN_HOURS}h)")
                    update_signal_decision(symbol, "rejected", "NARRATIVE_CD")
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
        is_valid, reason = pnl_service.validate(prediction, settings, symbol=symbol, news_client=news_client)
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
                        # ── Funding Rate filter ────────────────────────────────
                        _funding_ok = True
                        try:
                            _fr_url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit=1"
                            import urllib.request as _ureq
                            with _ureq.urlopen(_fr_url, timeout=4) as _fr_resp:
                                _fr_data = __import__('json').loads(_fr_resp.read())
                            if _fr_data:
                                _fr_val = float(_fr_data[0].get('fundingRate', 0))
                                if _fr_val > 0.0005:  # > 0.05% = longs muy apalancados → peligro
                                    _funding_ok = False
                                    print(f"VolSpike OVERRIDE {symbol}: funding rate={_fr_val:.4%} > 0.05% — skip (overleveraged longs)")
                                else:
                                    print(f"VolSpike OVERRIDE {symbol}: funding rate={_fr_val:.4%} ✓")
                        except Exception:
                            pass  # no futures pair or API error → allow entry
                        if (_spike >= _min_spike and _rsi < _max_rsi
                                and (not _need_trend or _trend_up) and _funding_ok):
                            # Check cooldown — no re-entry within 4h of last override
                            _last = _VOLSPIKE_COOLDOWN.get(symbol)
                            from datetime import datetime as _dt
                            _now_utc = _dt.now(timezone.utc)
                            if _last:
                                _hours_since = (_now_utc - _last).total_seconds() / 3600
                                if _hours_since < _VOLSPIKE_COOLDOWN_HOURS:
                                    print(f"VolSpike OVERRIDE {symbol}: cooldown active ({_hours_since:.1f}h < {_VOLSPIKE_COOLDOWN_HOURS}h) — skip")
                                else:
                                    # Check max 1 open override position for this symbol
                                    _open_count = position_manager.get_positions_count(symbol) if position_manager else 0
                                    if _open_count >= 1:
                                        print(f"VolSpike OVERRIDE {symbol}: already {_open_count} open position(s) — skip")
                                    else:
                                        volspike_override = True
                                        _VOLSPIKE_COOLDOWN[symbol] = _now_utc
                                        print(f"VolSpike OVERRIDE {symbol}: spike={_spike:.2f}x, RSI4h={_rsi:.1f}, trend={'UP' if _trend_up else 'DOWN'} → bypassing PnL veto (pred={ep:.2f}, prob={sp:.2f})")
                            else:
                                # First time — check no open position exists
                                _open_count = position_manager.get_positions_count(symbol) if position_manager else 0
                                if _open_count >= 1:
                                    print(f"VolSpike OVERRIDE {symbol}: already {_open_count} open position(s) — skip")
                                else:
                                    volspike_override = True
                                    _VOLSPIKE_COOLDOWN[symbol] = _dt.now(timezone.utc)
                                    print(f"VolSpike OVERRIDE {symbol}: spike={_spike:.2f}x, RSI4h={_rsi:.1f}, trend={'UP' if _trend_up else 'DOWN'} → bypassing PnL veto (pred={ep:.2f}, prob={sp:.2f})")
                    except Exception as _ve:
                        print(f"VolSpike override check failed for {symbol}: {_ve}")
            if not volspike_override:
                print(f"Skipping buy {symbol}: {reason} (pred={ep:.2f} USDT, prob={sp:.2f})")
                update_signal_decision(symbol, "rejected", "PNL_BAJO")
                continue
        if prediction and is_valid:
            print(f"Prediction OK for {symbol}: {prediction['expected_pnl']:.2f} USDT, prob={prediction['success_probability']:.2f}")

        # ── CoinGecko Trending boost ────────────────────────────────────
        if news_client:
            try:
                if news_client.is_trending(symbol):
                    strength = min(1.0, strength + 0.15)
                    print(f"CoinGecko Trending BOOST {symbol}: strength → {strength:.2f}")
            except Exception:
                pass

        # ── Kelly dynamic position sizing ───────────────────────────────
        sl_pct = getattr(settings, 'stop_loss_pct', 1.5) / 100.0
        tp_pct = getattr(settings, 'take_profit_pct', 2.5) / 100.0

        # Apply regime-specific SL/TP overrides
        if _regime and _regime.confirmed:
            if _regime_mode == "RANGE_SCALP":
                sl_pct = getattr(settings, 'range_scalp_sl_pct', 1.2) / 100.0
                tp_pct = getattr(settings, 'range_scalp_tp_pct', 1.5) / 100.0
                print(f"[Regime] RANGE_SCALP: SL={sl_pct*100:.1f}% TP={tp_pct*100:.1f}%")
            elif _regime_mode == "PRESERVATION":
                sl_pct = getattr(settings, 'preservation_sl_pct', 1.5) / 100.0
                tp_pct = getattr(settings, 'preservation_tp_pct', 3.0) / 100.0
                print(f"[Regime] PRESERVATION: SL={sl_pct*100:.1f}% TP={tp_pct*100:.1f}%")

        usdt_liquid_now = float(client.get_account_balance().get("USDT", usdt_liquid))
        trade_allocation = _kelly_position_size(
            settings, strength, prediction, usdt_liquid_now, sl_pct, tp_pct
        )
        # Regime Kelly multiplier — single application, no double-counting
        if _regime and _regime.confirmed and _regime_mode != "SPOT_BULL":
            _rmult = _regime.kelly_multiplier
            trade_allocation *= _rmult
            print(f"[Regime] {_regime_mode}: Kelly ×{_rmult} → ${trade_allocation:.2f} for {symbol}")

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
            # Register NARRATIVE cooldown BEFORE placing order to prevent race condition
            if symbol in narrative_set:
                from datetime import datetime as _dt3
                _NARRATIVE_BUY_COOLDOWN[symbol] = _dt3.now(timezone.utc)

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
                        _tok_cat = getattr(settings, 'token_categories', {}).get(symbol, {})
                        _cat_beh = _tok_cat.get('market_behavior', 'MIDCAP_ALT')
                        _cat_th  = getattr(settings, 'category_thresholds', {}).get(_cat_beh, {})
                        sl_pct = _cat_th.get('stop_loss_pct',  getattr(settings, 'stop_loss_pct',  1.5)) / 100.0
                        tp_pct = _cat_th.get('take_profit_pct', getattr(settings, 'take_profit_pct', 2.5)) / 100.0
                        sl_price = price * (1 - sl_pct)
                        tp_price = price * (1 + tp_pct)
                        print(f"Category SL/TP for {symbol} ({_cat_beh}): SL={sl_pct*100:.1f}% TP={tp_pct*100:.1f}%")

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


def _select_symbols(settings, news_client: NewsClient, client: BinanceClient, portfolio_manager, regime: str = "SPOT_BULL") -> List[Dict]:
    """Step 1: Resolve top buy symbols (as signal dicts) from signals or autonomous generation."""
    top_signals = _get_top_buy_symbols(settings, news_client, client, portfolio_manager, regime=regime)
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
        if not symbols_to_close:
            return
        print(f"Closing positions based on predictions: {symbols_to_close}")
        # ── Execute real sell orders for each symbol to close ─────────────
        from trade_utils import get_symbol_filters, align_quantity_to_step, is_quantity_valid
        for sym in symbols_to_close:
            try:
                balances = client.get_account_balance()
                asset = sym.replace("USDT", "")
                qty_held = float(balances.get(asset, 0))
                if qty_held <= 0:
                    print(f"Prediction close {sym}: no balance held, skipping sell")
                    continue
                filters = get_symbol_filters(client, sym)
                qty = align_quantity_to_step(qty_held, filters["step"])
                valid, reason = is_quantity_valid(qty, client.get_symbol_price(sym), filters)
                if not valid:
                    print(f"Prediction close {sym}: invalid qty {qty} — {reason}")
                    continue
                order = client.place_order(symbol=sym, side="SELL", quantity=qty)
                exit_price = float(order.get("fills", [{}])[0].get("price", 0) or
                                   client.get_symbol_price(sym))
                # Close position records in DB
                for pos in position_manager.get_all_positions().get(sym, []):
                    pnl = (exit_price - float(pos.entry_price)) * float(pos.quantity)
                    position_manager.close_position(
                        position_id=pos.position_id,
                        exit_price=exit_price,
                        pnl=pnl,
                        close_reason="prediction_reallocation",
                    )
                print(f"Prediction close {sym}: sold {qty} @ {exit_price:.4f}, pnl estimated")
            except Exception as sell_err:
                print(f"Prediction close {sym}: sell failed — {sell_err}")
        time.sleep(1)
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

    # ── Log Fear & Greed state ──────────────────────────────────────────────
    try:
        _fng = news_client.get_fear_greed()
        _fng_mod = news_client.get_fear_greed_modifier()
        print(f"Fear & Greed Index: {_fng.get('value')} ({_fng.get('label')}) → prob_modifier={_fng_mod:+.2f}")
        _trending = news_client.get_trending_symbols()
        if _trending:
            print(f"CoinGecko Trending: {_trending}")
    except Exception as _fng_err:
        print(f"F&G/Trending check skipped: {_fng_err}")

    if not settings.binance_use_testnet and not settings.allow_real_trading:
        print("Real trading is disabled. Skipping execute_once.")
        return {"top_symbols": [], "sell_orders": [], "buy_orders": []}

    if settings.agent_mode != "auto":
        print("Agent mode is not auto. Skipping execute_once.")
        return {"top_symbols": [], "sell_orders": [], "buy_orders": []}

    # ── Regime detection for execute_once (needed by Step 0.5 and Step 1) ──────
    _regime_exe: MarketRegime = None
    _regime_mode = "SPOT_BULL"
    if getattr(settings, 'market_regime_enabled', True):
        try:
            _regime_exe = get_regime(client, settings=settings)
            _regime_mode = _regime_exe.mode
            print(f"[Regime] execute_once: {_regime_mode} | confirmed={_regime_exe.confirmed}")
        except Exception as _re_exe:
            print(f"[Regime] execute_once detection failed: {_re_exe}")

    # ── Step 0: SL/TP monitor — runs every cycle before any new trades ─────────
    sl_tp_closes = _check_sl_tp_positions(client, settings, position_manager)
    if sl_tp_closes:
        print(f"SL/TP closed {len(sl_tp_closes)} position(s): {[c['symbol'] for c in sl_tp_closes]}")

    # ── Step 0.5: Futures positions check + auto-fund on FUTURES_SHORT ──────────
    futures_closes = []
    if getattr(settings, 'futures_enabled', False):
        try:
            _fc = _get_futures_client(settings)

            # Auto-fund: when regime is FUTURES_SHORT, ensure futures wallet has enough margin
            if _regime_mode == "FUTURES_SHORT" and _regime_exe and _regime_exe.confirmed:
                _f_usdt_per_trade = getattr(settings, 'futures_usdt_per_trade', 20.0)
                _f_max_shorts     = getattr(settings, 'futures_max_shorts', 2)
                _f_required       = _f_usdt_per_trade * _f_max_shorts
                _funded = _fc.check_and_fund(
                    required_usdt  = _f_required,
                    spot_api_key   = settings.binance_api_key,
                    spot_api_secret= settings.binance_api_secret,
                )
                if not _funded:
                    print(f"[Futures] WARNING: could not fund futures wallet (need ${_f_required:.2f}) — shorts will be skipped")

            # A3: Auto-close ALL shorts if regime switched OUT of bear modes
            _open_shorts = list(_fc._open_positions.keys())
            if _open_shorts and _regime_mode not in ("PRESERVATION", "FUTURES_SHORT"):
                print(f"[Futures] Regime is {_regime_mode} — closing ALL {len(_open_shorts)} shorts")
                for _sym in _open_shorts:
                    try:
                        _pos = _fc._open_positions[_sym]
                        _result = _fc.close_short(_sym, _pos.qty, reason="regime_change")
                        futures_closes.append({"symbol": _sym, "reason": "regime_change", "result": _result})
                        print(f"[Futures] Closed short {_sym} due to regime change → {_regime_mode}")
                    except Exception as _ce:
                        print(f"[Futures] Failed to close {_sym} on regime change: {_ce}")

            # Bidirectional: return idle futures funds to spot when not in SHORT mode
            if _regime_mode not in ("PRESERVATION", "FUTURES_SHORT"):
                try:
                    _fc.return_idle_funds_to_spot(
                        reserve_usdt    = 5.0,
                        spot_api_key    = settings.binance_api_key,
                        spot_api_secret = settings.binance_api_secret,
                    )
                except Exception as _rfe:
                    print(f"[Futures] return_idle_funds_to_spot error: {_rfe}")

            # Normal SL/TP cycle check
            _cycle_closes = _fc.check_positions()
            if _cycle_closes:
                futures_closes.extend(_cycle_closes)
                print(f"[Futures] Closed {len(_cycle_closes)} position(s) via SL/TP: {_cycle_closes}")
        except Exception as _fce:
            print(f"[Futures] check_positions error: {_fce}")

    # ── Step 1: Determine top symbols ─────────────────────────────────────────
    top_signals = _select_symbols(settings, news_client, client, portfolio_manager, regime=_regime_mode)
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
    buy_orders = _buy_top_symbols(client, settings, top_signals, portfolio_manager, position_manager, precomputed_predictions=predictions, news_client=news_client)

    print("Completed cycle.")
    return {"top_symbols": top_symbols, "sell_orders": sell_orders,
            "buy_orders": buy_orders, "sl_tp_closes": sl_tp_closes,
            "futures_closes": futures_closes}


if __name__ == '__main__':
    settings = get_settings()
    execute_once()

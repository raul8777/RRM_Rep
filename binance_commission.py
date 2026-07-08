"""
Binance Commission Module

Fuente única de verdad para todas las comisiones de Binance en el sistema.
Soporta SPOT y FUTURES, con/sin descuento BNB, y expone helpers para
PnL neto, EV neto de Kelly y expected PnL neto.

Todas las operaciones de trading deben usar este módulo para garantizar
consistencia entre producción, backtest y predictor.
"""

from dataclasses import dataclass
from typing import Optional, Dict, Any
from decimal import Decimal
from config_helpers import get_param as _cfg_param


DEFAULT_SPOT_RATE = 0.001        # 0.1% taker spot
DEFAULT_FUTURES_RATE = 0.0004    # 0.04% taker futures
DEFAULT_BNB_SPOT_RATE = 0.0005   # 0.05% spot con BNB
DEFAULT_BNB_FUTURES_RATE = 0.0002  # 0.02% futures con BNB


@dataclass(frozen=True)
class CommissionConfig:
    """Tasas de comisión activas para el sistema."""
    spot_rate: float
    futures_rate: float
    bnb_spot_rate: float
    bnb_futures_rate: float
    use_bnb: bool
    slippage_pct: float = 0.0005  # 0.05% default slippage estimate

    def rate_for(self, trade_type: str) -> float:
        """Return active rate for trade_type ('spot' or 'futures')."""
        tt = trade_type.lower()
        if tt == 'spot':
            return self.bnb_spot_rate if self.use_bnb else self.spot_rate
        if tt in ('futures', 'future'):
            return self.bnb_futures_rate if self.use_bnb else self.futures_rate
        raise ValueError(f"trade_type must be 'spot' or 'futures', got {trade_type}")


def get_config(settings: Optional[Any] = None) -> CommissionConfig:
    """Build CommissionConfig from settings object or environment defaults.

    Commission rates are controlled by Binance, not by backtest or local config.
    The canonical source of truth is the live account info fetched via
    refresh_config_from_binance(); settings are only a fallback/cache.
    """
    if settings is None:
        try:
            from config import get_settings
            settings = get_settings()
        except Exception:
            settings = None

    return CommissionConfig(
        spot_rate=_cfg_param(settings, 'binance_commission_rate'),
        futures_rate=_cfg_param(settings, 'futures_commission_rate'),
        bnb_spot_rate=_cfg_param(settings, 'bnb_spot_commission_rate'),
        bnb_futures_rate=_cfg_param(settings, 'bnb_futures_commission_rate'),
        use_bnb=_cfg_param(settings, 'use_bnb_for_commission'),
        slippage_pct=_cfg_param(settings, 'slippage_pct'),
    )


def refresh_config_from_binance(
    settings,
    spot_client=None,
    futures_client=None,
    db_client=None,
    force: bool = False,
) -> Dict[str, Any]:
    """Fetch live commission rates from Binance and update settings + DB.

    Returns a dict with fetched rates, use_bnb flag, and any change alert.
    If a change is detected against the previously persisted config, a
    drift alert is recorded for human review.
    """
    import json
    result = {
        'spot_rate': None,
        'futures_rate': None,
        'use_bnb': None,
        'alert_id': None,
        'error': None,
    }
    try:
        old_cfg = load_config_from_db(db_client) or get_config(settings)

        spot_rate = None
        futures_rate = None
        if spot_client is not None:
            try:
                spot_rate = float(spot_client.get_commission_rate())
            except Exception as e:
                result['error'] = f"spot_rate: {e}"
        if futures_client is not None:
            try:
                futures_rate = float(futures_client.get_commission_rate())
            except Exception as e:
                result['error'] = (result['error'] or '') + f" futures_rate: {e}"

        # If API calls fail, keep current settings values.
        if spot_rate is None:
            spot_rate = _cfg_param(settings, 'binance_commission_rate')
        if futures_rate is None:
            futures_rate = _cfg_param(settings, 'futures_commission_rate')

        # BNB discount: the spot /api/v3/account endpoint does not expose a
        # BNB-discount flag, but when it is active the reported spot rate is
        # 0.0005 (0.05%). If the API reports that rate, we enable BNB. If it
        # reports the standard 0.001 rate we keep the current setting, because
        # the account may still have BNB discount enabled and the flag is not
        # visible via this endpoint. The definitive BNB status is managed by
        # auto_manage_bnb_discount() which inspects balances and recent fills.
        use_bnb = _cfg_param(settings, 'use_bnb_for_commission')
        if spot_rate is not None and spot_rate <= DEFAULT_BNB_SPOT_RATE + 1e-9:
            use_bnb = True

        # Update settings in memory
        settings.binance_commission_rate = spot_rate
        settings.futures_commission_rate = futures_rate
        settings.use_bnb_for_commission = use_bnb

        new_cfg = get_config(settings)
        if db_client is not None:
            persist_config(db_client, new_cfg)
            record_rate_change(db_client, old_cfg, new_cfg, changed_by='binance_api')

            # Detect meaningful change and create a drift alert
            _EPS = 1e-9
            _changed = (
                force or
                abs(old_cfg.spot_rate - new_cfg.spot_rate) > _EPS or
                abs(old_cfg.futures_rate - new_cfg.futures_rate) > _EPS or
                old_cfg.use_bnb != new_cfg.use_bnb
            )
            if _changed:
                try:
                    _detail = {
                        'spot_rate': new_cfg.spot_rate,
                        'futures_rate': new_cfg.futures_rate,
                        'use_bnb': new_cfg.use_bnb,
                    }
                    _old_detail = {
                        'spot_rate': old_cfg.spot_rate,
                        'futures_rate': old_cfg.futures_rate,
                        'use_bnb': old_cfg.use_bnb,
                    }
                    db_client.execute_update(
                        """INSERT INTO config_drift_alerts
                           (severity, param, expected, actual, source, acknowledged, notes)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)
                           RETURNING id""",
                        ('warning', 'binance_commission_rate',
                         json.dumps(_old_detail), json.dumps(_detail),
                         'binance_api_refresh', False,
                         'Commission rates refreshed from Binance API')
                    )
                    rows = db_client.execute_query(
                        "SELECT id FROM config_drift_alerts WHERE param=%s AND source=%s ORDER BY id DESC LIMIT 1",
                        ('binance_commission_rate', 'binance_api_refresh')
                    )
                    if rows:
                        result['alert_id'] = rows[0].get('id')
                except Exception:
                    pass

        result['spot_rate'] = spot_rate
        result['futures_rate'] = futures_rate
        result['use_bnb'] = use_bnb
    except Exception as e:
        result['error'] = str(e)
    return result


def _quantize(value: float, step: float) -> float:
    """Truncate to step size (like LOT_SIZE) — fees are rounded down to precision."""
    if step <= 0 or value <= 0:
        return 0.0
    return float((Decimal(str(value)) // Decimal(str(step))) * Decimal(str(step)))


def entry_commission(
    quantity: float,
    price: float,
    trade_type: str = 'spot',
    settings: Optional[Any] = None,
) -> float:
    """Commission paid on entry order (USDT)."""
    cfg = get_config(settings)
    return quantity * price * cfg.rate_for(trade_type)


def exit_commission(
    quantity: float,
    price: float,
    trade_type: str = 'spot',
    settings: Optional[Any] = None,
) -> float:
    """Commission paid on exit order (USDT)."""
    cfg = get_config(settings)
    return quantity * price * cfg.rate_for(trade_type)


def total_commission(
    quantity: float,
    entry_price: float,
    exit_price: float,
    trade_type: str = 'spot',
    settings: Optional[Any] = None,
) -> float:
    """Total entry + exit commission (USDT)."""
    return (
        entry_commission(quantity, entry_price, trade_type, settings)
        + exit_commission(quantity, exit_price, trade_type, settings)
    )


def net_pnl(
    gross_pnl: float,
    quantity: float,
    entry_price: float,
    exit_price: float,
    trade_type: str = 'spot',
    settings: Optional[Any] = None,
) -> float:
    """Return PnL after deducting both sides of commission."""
    return gross_pnl - total_commission(quantity, entry_price, exit_price, trade_type, settings)


def net_pnl_with_slippage(
    gross_pnl: float,
    quantity: float,
    entry_price: float,
    exit_price: float,
    trade_type: str = 'spot',
    settings: Optional[Any] = None,
) -> float:
    """Return PnL after commission + slippage (used in backtest)."""
    cfg = get_config(settings)
    comm = total_commission(quantity, entry_price, exit_price, trade_type, settings)
    notional = quantity * (entry_price + exit_price) / 2.0
    slippage = notional * cfg.slippage_pct
    return gross_pnl - comm - slippage


def expected_pnl_after_fees(
    predicted_gross_pnl: float,
    position_size_usdt: float,
    trade_type: str = 'spot',
    settings: Optional[Any] = None,
) -> float:
    """
    Adjust predictor gross expected PnL by estimated round-trip commission.
    For SPOT, fee is ~2× rate on notional.
    For FUTURES, fee is ~2× rate on notional; leverage does not change commission.
    """
    cfg = get_config(settings)
    rate = cfg.rate_for(trade_type)
    # Estimated round-trip fee as percentage of position size
    round_trip_fee_pct = rate * 2.0
    estimated_fee = position_size_usdt * round_trip_fee_pct
    return predicted_gross_pnl - estimated_fee


def kelly_ev_after_fees(
    prob: float,
    tp_pct: float,
    sl_pct: float,
    position_size_usdt: float,
    trade_type: str = 'spot',
    settings: Optional[Any] = None,
) -> float:
    """
    Expected value of a trade after Binance fees, used in Kelly sizing.
    prob: success probability
    tp_pct: take-profit percent (e.g. 0.025 for 2.5%)
    sl_pct: stop-loss percent (e.g. 0.015 for 1.5%)
    position_size_usdt: notional size at entry

    Returns EV in USDT (can be negative if fees eat the edge).
    """
    cfg = get_config(settings)
    rate = cfg.rate_for(trade_type)
    win_amount = position_size_usdt * tp_pct
    loss_amount = position_size_usdt * sl_pct
    gross_ev = prob * win_amount - (1 - prob) * loss_amount
    round_trip_fee = position_size_usdt * rate * 2.0
    return gross_ev - round_trip_fee


def net_return_pct(
    gross_return_pct: float,
    trade_type: str = 'spot',
    settings: Optional[Any] = None,
) -> float:
    """Subtract round-trip commission percentage from gross return percentage."""
    cfg = get_config(settings)
    return gross_return_pct - cfg.rate_for(trade_type) * 200.0


def record_rate_change(db_client: Any, old: CommissionConfig, new: CommissionConfig, changed_by: str = 'system') -> None:
    """Audit a commission rate change in trades_audit for traceability."""
    if not db_client:
        return
    try:
        import json
        detail = {
            'old': {
                'spot_rate': old.spot_rate,
                'futures_rate': old.futures_rate,
                'use_bnb': old.use_bnb,
            },
            'new': {
                'spot_rate': new.spot_rate,
                'futures_rate': new.futures_rate,
                'use_bnb': new.use_bnb,
            },
            'changed_by': changed_by,
        }
        db_client.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
            "VALUES (%s, %s, %s, %s, %s)",
            ('__config__', 'system', 'commission_rate_change', json.dumps(detail), False)
        )
    except Exception:
        pass


def persist_config(db_client: Any, cfg: CommissionConfig) -> None:
    """Persist current commission config in trading_runtime_state for restart safety."""
    if not db_client:
        return
    try:
        import json
        db_client.execute_update(
            """INSERT INTO trading_runtime_state (key, value, updated_at)
               VALUES (%s, %s, NOW())
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
            ('commission_config', json.dumps({
                'spot_rate': cfg.spot_rate,
                'futures_rate': cfg.futures_rate,
                'bnb_spot_rate': cfg.bnb_spot_rate,
                'bnb_futures_rate': cfg.bnb_futures_rate,
                'use_bnb': cfg.use_bnb,
                'slippage_pct': cfg.slippage_pct,
            }))
        )
    except Exception:
        pass


def load_config_from_db(db_client: Any) -> Optional[CommissionConfig]:
    """Load commission config from trading_runtime_state if available."""
    if not db_client:
        return None
    try:
        import json
        rows = db_client.execute_query(
            "SELECT value FROM trading_runtime_state WHERE key = %s",
            ('commission_config',)
        )
        if not rows:
            return None
        v = rows[0]['value']
        if isinstance(v, str):
            v = json.loads(v)
        return CommissionConfig(
            spot_rate=float(v['spot_rate']),
            futures_rate=float(v['futures_rate']),
            bnb_spot_rate=float(v['bnb_spot_rate']),
            bnb_futures_rate=float(v['bnb_futures_rate']),
            use_bnb=bool(v['use_bnb']),
            slippage_pct=float(v.get('slippage_pct', 0.0005)),
        )
    except Exception:
        return None


# ── BNB discount detection / auto-conversion helpers ─────────────────────────

def detect_bnb_discount_from_orders(orders: list, trade_type: str = 'spot') -> bool:
    """
    Infer whether BNB discount is active by inspecting commissionAsset of recent orders.
    Returns True if majority of orders paid commission in BNB.
    """
    if not orders:
        return False
    bnb_commissions = 0
    total_commissions = 0
    for o in orders:
        for fill in o.get('fills', []):
            comm_asset = fill.get('commissionAsset', '')
            if comm_asset:
                total_commissions += 1
                if comm_asset == 'BNB':
                    bnb_commissions += 1
    if total_commissions == 0:
        return False
    return (bnb_commissions / total_commissions) >= 0.8


def estimate_bnb_buffer_needed(settings, trade_type: str = 'spot', days: int = 7) -> float:
    """
    Estimate USDT worth of BNB needed to cover commission burn for `days`.
    Uses average commission per trade from recent closed trades.
    """
    from db_client import DatabaseClient
    db_url = getattr(settings, 'db_url', None)
    if not db_url:
        return max(
            float(getattr(settings, 'bnb_min_balance_usd', 5.0)),
            5.0
        )
    db = DatabaseClient(db_url) if 'DatabaseClient' in globals() else None
    if db is None:
        try:
            from db_client import DatabaseClient
            db = DatabaseClient(db_url)
        except Exception:
            return float(getattr(settings, 'bnb_min_balance_usd', 5.0))
    try:
        query = """
            SELECT AVG(COALESCE(entry_commission, 0) + COALESCE(exit_commission, 0)) as avg_comm,
                   COUNT(*) as cnt
            FROM trades_closed
            WHERE exit_time >= NOW() - INTERVAL '7 days'
              AND metadata->>'trade_type' = %s
        """
        rows = db.execute_query(query, (trade_type,))
        row = rows[0] if rows else {}
        avg_comm = float(row.get('avg_comm') or 0)
        cnt = int(row.get('cnt') or 0)
        if cnt == 0 or avg_comm == 0:
            # Fallback: estimate from daily trade count and default size
            avg_size = float(_cfg_param(settings, 'kelly_base_usdt'))
            rate = get_config(settings).rate_for(trade_type)
            avg_comm = avg_size * rate * 2.0
            cnt = 5 * 7  # assume 5 trades/day
        daily_comm = avg_comm * cnt / 7.0
        return max(daily_comm * days, float(getattr(settings, 'bnb_min_balance_usd', 5.0)))
    except Exception:
        return 0.0


def audit_bnb_status(db_client: Any, spot_bnb: float, futures_bnb: float,
                     spot_buffer: float, futures_buffer: float, use_bnb: bool,
                     bnb_price: float) -> None:
    """Record BNB status snapshot to trades_audit for traceability."""
    if not db_client:
        return
    try:
        import json
        db_client.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
            "VALUES (%s, %s, %s, %s, %s)",
            ('__config__', 'system', 'bnb_status_snapshot',
             json.dumps({
                 'spot_bnb_balance': spot_bnb,
                 'futures_bnb_balance': futures_bnb,
                 'spot_buffer_needed': spot_buffer,
                 'futures_buffer_needed': futures_buffer,
                 'bnb_price_usdt': bnb_price,
                 'use_bnb_for_commission': use_bnb,
             }), False)
        )
    except Exception:
        pass


def record_bnb_operation(db_client: Any, operation: str, detail: Dict[str, Any]) -> None:
    """Persist a BNB management action (buy, transfer, shortfall, failure) to trades_audit."""
    if not db_client:
        return
    try:
        import json
        db_client.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
            "VALUES (%s, %s, %s, %s, %s)",
            ('__config__', 'system', operation,
             json.dumps(detail), False)
        )
    except Exception:
        pass


def get_bnb_reserve_usdt(settings, trade_type: Optional[str] = None) -> Dict[str, float]:
    """Return USD value of BNB the system should keep reserved per wallet.

    If trade_type is None, returns both spot and futures reserves plus total.
    """
    spot_buffer = estimate_bnb_buffer_needed(settings, 'spot', settings.bnb_buffer_days)
    futures_buffer = estimate_bnb_buffer_needed(settings, 'futures', settings.bnb_buffer_days)
    extra = float(getattr(settings, 'bnb_extra_buffer_usd', 10.0))
    result = {
        'spot_reserve_usdt': spot_buffer + extra,
        'futures_reserve_usdt': futures_buffer + extra,
        'total_reserve_usdt': spot_buffer + futures_buffer + 2 * extra,
    }
    if trade_type and trade_type.lower() in ('spot', 'futures'):
        key = f"{trade_type.lower()}_reserve_usdt"
        return {key: result[key], 'total_reserve_usdt': result[key]}
    return result


def auto_manage_bnb_discount(
    settings,
    spot_client=None,
    futures_client=None,
    db_client=None,
    force: bool = False,
    regime_mode: str = None,
) -> Dict[str, Any]:
    """
    Auto-detect BNB balance, top-up if needed, and activate BNB commission discount.
    Returns status dict with actions taken for full traceability.
    """
    if regime_mode is None:
        regime_mode = getattr(settings, 'current_regime_mode', 'SPOT_BULL') or 'SPOT_BULL'
    result = {
        'spot_bnb': 0.0,
        'futures_bnb': 0.0,
        'bnb_price': 0.0,
        'spot_buffer': 0.0,
        'futures_buffer': 0.0,
        'spot_reserve_usdt': 0.0,
        'futures_reserve_usdt': 0.0,
        'total_reserve_usdt': 0.0,
        'spot_usdt_available': 0.0,
        'futures_usdt_available': 0.0,
        'shortfall_usdt': 0.0,
        'usdt_source': 'none',
        'usdt_transferred_from_futures': 0.0,
        'usdt_transferred_from_spot': 0.0,
        'regime_mode': regime_mode,
        'spot_action': 'none',
        'futures_action': 'none',
        'use_bnb': False,
        'errors': [],
    }
    try:
        # Get BNB price
        if spot_client:
            try:
                result['bnb_price'] = float(spot_client.get_symbol_price('BNBUSDT'))
            except Exception as e:
                result['errors'].append(f'bnb_price: {e}')
        if not result['bnb_price'] and futures_client:
            try:
                result['bnb_price'] = float(futures_client.get_price('BNBUSDT'))
            except Exception as e:
                result['errors'].append(f'futures_bnb_price: {e}')
        if not result['bnb_price']:
            return result

        # Get BNB balances
        if spot_client:
            try:
                balances = spot_client.get_account_balance()
                result['spot_bnb'] = float(balances.get('BNB', 0))
            except Exception as e:
                result['errors'].append(f'spot_bnb_balance: {e}')
        if futures_client:
            try:
                account = futures_client.get_account_info()
                for asset in account.get('assets', []):
                    if asset.get('asset') == 'BNB':
                        result['futures_bnb'] = float(asset.get('availableBalance', 0))
                        break
            except Exception as e:
                result['errors'].append(f'futures_bnb_balance: {e}')

        # Estimate buffers and reserves
        result['spot_buffer'] = estimate_bnb_buffer_needed(settings, 'spot', settings.bnb_buffer_days)
        result['futures_buffer'] = estimate_bnb_buffer_needed(settings, 'futures', settings.bnb_buffer_days)
        _reserves = get_bnb_reserve_usdt(settings)
        result['spot_reserve_usdt'] = _reserves['spot_reserve_usdt']
        result['futures_reserve_usdt'] = _reserves['futures_reserve_usdt']
        result['total_reserve_usdt'] = _reserves['total_reserve_usdt']

        spot_bnb_usd = result['spot_bnb'] * result['bnb_price']
        futures_bnb_usd = result['futures_bnb'] * result['bnb_price']
        min_usd = float(settings.bnb_min_balance_usd)

        auto_enabled = bool(getattr(settings, 'auto_bnb_for_commission', True)) or force
        if not auto_enabled:
            # Only audit, do not convert
            audit_bnb_status(db_client, result['spot_bnb'], result['futures_bnb'],
                             result['spot_buffer'], result['futures_buffer'], False,
                             result['bnb_price'])
            return result

        # Targets: buffer + extra USD (default 10 USD) to avoid running out
        extra_usd = float(getattr(settings, 'bnb_extra_buffer_usd', 10.0))
        target_spot_usd = result['spot_buffer'] + extra_usd
        target_futures_usd = result['futures_buffer'] + extra_usd

        # Calculate needed BNB for each wallet (rounded UP to 3 decimals for BNBUSDT LOT_SIZE)
        import math
        def _ceil_bnb(usd):
            bnb = usd / result['bnb_price']
            return math.ceil(bnb / 0.001) * 0.001

        needed_spot_bnb = max(0.0, _ceil_bnb(target_spot_usd - spot_bnb_usd))
        needed_futures_bnb = max(0.0, _ceil_bnb(target_futures_usd - futures_bnb_usd))

        # Buy total BNB in one SPOT order so we can cover both wallets in this cycle.
        # BNBUSDT min_notional ~10 USDT, so if any wallet needs BNB, buy at least that.
        total_buy_bnb = round(max(needed_spot_bnb + needed_futures_bnb, 0.0), 3)
        min_buy_notional_bnb = max(10.0 / result['bnb_price'], 0.001)
        if total_buy_bnb > 0 and total_buy_bnb < min_buy_notional_bnb:
            total_buy_bnb = round(min_buy_notional_bnb, 3)

        # Check available USDT in both wallets before buying
        buy_usdt_needed = total_buy_bnb * result['bnb_price']
        spot_usdt_available = 0.0
        futures_usdt_available = 0.0
        if spot_client:
            try:
                spot_usdt_available = float(spot_client.get_account_balance().get('USDT', 0))
            except Exception as e:
                result['errors'].append(f'spot_usdt_balance: {e}')
        if futures_client:
            try:
                futures_usdt_available = float(futures_client.get_available_usdt())
            except Exception as e:
                result['errors'].append(f'futures_usdt_balance: {e}')
        result['spot_usdt_available'] = spot_usdt_available
        result['futures_usdt_available'] = futures_usdt_available

        # Determine USDT source preference based on active regime:
        # - In SPOT-focused regimes, keep spot liquid -> prefer futures source.
        # - In FUTURES-focused regimes, keep futures liquid -> prefer spot source.
        _regime = regime_mode.upper()
        if _regime in ('FUTURES_SHORT', 'PRESERVATION'):
            primary_source, secondary_source = 'spot', 'futures'
        else:
            primary_source, secondary_source = 'futures', 'spot'

        # Attempt to source USDT from the preferred wallet, then fallback
        if spot_client and total_buy_bnb >= 0.001:
            usdt_in_spot = spot_usdt_available
            usdt_in_futures = futures_usdt_available
            if usdt_in_spot < buy_usdt_needed:
                shortage = buy_usdt_needed - usdt_in_spot
                # First, try the preferred source (futures) when possible
                if futures_client and primary_source == 'futures' and shortage > 0:
                    _max_from_futures = max(0.0, futures_usdt_available - extra_usd)
                    _transfer_usdt = min(shortage, _max_from_futures)
                    if _transfer_usdt >= 1.0:
                        record_bnb_operation(db_client, 'usdt_futures_to_spot_attempt', {
                            'amount': _transfer_usdt,
                            'reason': 'bnb_top_up',
                            'regime_mode': regime_mode,
                            'futures_available_before': futures_usdt_available,
                            'spot_usdt_before': spot_usdt_available,
                        })
                        _ok = futures_client.transfer_to_spot(_transfer_usdt)
                        if _ok:
                            result['usdt_transferred_from_futures'] = _transfer_usdt
                            result['usdt_source'] = 'futures'
                            record_bnb_operation(db_client, 'usdt_futures_to_spot_success', {
                                'amount': _transfer_usdt,
                                'reason': 'bnb_top_up',
                                'regime_mode': regime_mode,
                            })
                        else:
                            result['errors'].append('futures_to_spot_transfer_failed')
                            record_bnb_operation(db_client, 'usdt_futures_to_spot_failed', {
                                'amount': _transfer_usdt,
                                'reason': 'bnb_top_up',
                                'regime_mode': regime_mode,
                            })
                elif futures_client and primary_source == 'spot' and shortage > 0:
                    # Futures is the fallback when spot is preferred but insufficient
                    _max_from_futures = max(0.0, futures_usdt_available - extra_usd)
                    _transfer_usdt = min(shortage, _max_from_futures)
                    if _transfer_usdt >= 1.0:
                        record_bnb_operation(db_client, 'usdt_futures_to_spot_attempt', {
                            'amount': _transfer_usdt,
                            'reason': 'bnb_top_up',
                            'regime_mode': regime_mode,
                            'futures_available_before': futures_usdt_available,
                            'spot_usdt_before': spot_usdt_available,
                        })
                        _ok = futures_client.transfer_to_spot(_transfer_usdt)
                        if _ok:
                            result['usdt_transferred_from_futures'] = _transfer_usdt
                            result['usdt_source'] = 'futures_fallback'
                            record_bnb_operation(db_client, 'usdt_futures_to_spot_success', {
                                'amount': _transfer_usdt,
                                'reason': 'bnb_top_up',
                                'regime_mode': regime_mode,
                            })
                        else:
                            result['errors'].append('futures_to_spot_transfer_failed')
                            record_bnb_operation(db_client, 'usdt_futures_to_spot_failed', {
                                'amount': _transfer_usdt,
                                'reason': 'bnb_top_up',
                                'regime_mode': regime_mode,
                            })
                # Re-check spot balance after any transfer
                try:
                    spot_usdt_available = float(spot_client.get_account_balance().get('USDT', 0))
                    result['spot_usdt_available'] = spot_usdt_available
                except Exception as e:
                    result['errors'].append(f'spot_usdt_recheck_after_transfer: {e}')

            if spot_usdt_available >= buy_usdt_needed:
                result['usdt_source'] = result['usdt_source'] if result['usdt_source'] != 'none' else 'spot'
                record_bnb_operation(db_client, 'bnb_buy_attempt', {
                    'bnb_quantity': total_buy_bnb,
                    'bnb_price': result['bnb_price'],
                    'usdt_needed': buy_usdt_needed,
                    'spot_usdt_available': spot_usdt_available,
                    'futures_usdt_available': futures_usdt_available,
                    'regime_mode': regime_mode,
                })
                try:
                    spot_client.create_market_order('BNBUSDT', 'BUY', quantity=total_buy_bnb)
                    result['spot_action'] = f'bought_{total_buy_bnb:.6f}_bnb'
                    record_bnb_operation(db_client, 'bnb_buy_success', {
                        'bnb_quantity': total_buy_bnb,
                        'bnb_price': result['bnb_price'],
                        'usdt_needed': buy_usdt_needed,
                        'regime_mode': regime_mode,
                    })
                except Exception as e:
                    result['errors'].append(f'spot_buy_bnb: {e}')
                    result['spot_action'] = 'buy_failed'
                    record_bnb_operation(db_client, 'bnb_buy_failed', {
                        'bnb_quantity': total_buy_bnb,
                        'bnb_price': result['bnb_price'],
                        'error': str(e),
                        'regime_mode': regime_mode,
                    })
            else:
                result['spot_action'] = 'buy_skipped_insufficient_usdt'
                result['shortfall_usdt'] = max(0.0, buy_usdt_needed - spot_usdt_available)
                result['usdt_source'] = 'insufficient'
                record_bnb_operation(db_client, 'bnb_liquidity_shortfall', {
                    'bnb_quantity_needed': total_buy_bnb,
                    'bnb_price': result['bnb_price'],
                    'usdt_needed': buy_usdt_needed,
                    'spot_usdt_available': spot_usdt_available,
                    'futures_usdt_available': futures_usdt_available,
                    'usdt_transferred_from_futures': result['usdt_transferred_from_futures'],
                    'shortfall_usdt': result['shortfall_usdt'],
                    'regime_mode': regime_mode,
                })

        # Re-read SPOT balance after purchase
        if spot_client:
            try:
                balances = spot_client.get_account_balance()
                result['spot_bnb'] = float(balances.get('BNB', 0))
                spot_bnb_usd = result['spot_bnb'] * result['bnb_price']
            except Exception as e:
                result['errors'].append(f'spot_bnb_recheck: {e}')

        # Transfer FUTURES portion from SPOT if possible, keeping target_spot intact
        if futures_client and spot_client and needed_futures_bnb >= 0.001:
            # Keep at least target_spot BNB in spot wallet; transfer any excess above that
            reserve_spot_bnb = _ceil_bnb(target_spot_usd)
            available_for_futures = max(0.0, result['spot_bnb'] - reserve_spot_bnb)
            transfer_bnb = round(min(needed_futures_bnb, available_for_futures), 3)
            if transfer_bnb >= 0.001:
                record_bnb_operation(db_client, 'bnb_transfer_attempt', {
                    'bnb_quantity': transfer_bnb,
                    'bnb_price': result['bnb_price'],
                    'from': 'spot',
                    'to': 'futures',
                })
                try:
                    spot_client.client.universal_transfer(
                        type='MAIN_UMFUTURE',
                        asset='BNB',
                        amount=transfer_bnb,
                    )
                    result['futures_action'] = f'transferred_{transfer_bnb:.6f}_bnb_to_futures'
                    record_bnb_operation(db_client, 'bnb_transfer_success', {
                        'bnb_quantity': transfer_bnb,
                        'bnb_price': result['bnb_price'],
                        'from': 'spot',
                        'to': 'futures',
                    })
                except Exception as e:
                    result['errors'].append(f'futures_transfer_bnb: {e}')
                    result['futures_action'] = 'transfer_failed'
                    record_bnb_operation(db_client, 'bnb_transfer_failed', {
                        'bnb_quantity': transfer_bnb,
                        'bnb_price': result['bnb_price'],
                        'error': str(e),
                    })

        # Re-read balances after transfer
        if spot_client:
            try:
                balances = spot_client.get_account_balance()
                result['spot_bnb'] = float(balances.get('BNB', 0))
                spot_bnb_usd = result['spot_bnb'] * result['bnb_price']
            except Exception as e:
                result['errors'].append(f'spot_bnb_recheck2: {e}')
        if futures_client:
            try:
                account = futures_client.get_account_info()
                for asset in account.get('assets', []):
                    if asset.get('asset') == 'BNB':
                        result['futures_bnb'] = float(asset.get('availableBalance', 0))
                        futures_bnb_usd = result['futures_bnb'] * result['bnb_price']
                        break
            except Exception as e:
                result['errors'].append(f'futures_bnb_recheck: {e}')

        # Activate BNB discount only if both wallets have sufficient target buffer
        # and no recent top-up failed (avoid paying standard fees while believing discount is active)
        _topup_failed = result['spot_action'].endswith('_failed') or result['futures_action'].endswith('_failed')
        if (spot_bnb_usd >= target_spot_usd and futures_bnb_usd >= target_futures_usd
                and spot_bnb_usd > 0 and futures_bnb_usd > 0 and not _topup_failed):
            result['use_bnb'] = True

        # Persist and audit
        cfg = get_config(settings)
        new_cfg = CommissionConfig(
            spot_rate=cfg.spot_rate,
            futures_rate=cfg.futures_rate,
            bnb_spot_rate=cfg.bnb_spot_rate,
            bnb_futures_rate=cfg.bnb_futures_rate,
            use_bnb=result['use_bnb'],
            slippage_pct=cfg.slippage_pct,
        )
        if db_client:
            persist_config(db_client, new_cfg)
            record_rate_change(db_client, cfg, new_cfg, changed_by='auto_bnb_manager')
        audit_bnb_status(db_client, result['spot_bnb'], result['futures_bnb'],
                         result['spot_buffer'], result['futures_buffer'], result['use_bnb'],
                         result['bnb_price'])

        # Refresh live Binance commission rates now that BNB status is known
        try:
            refresh_config_from_binance(
                settings, spot_client=spot_client, futures_client=futures_client,
                db_client=db_client, force=False,
            )
        except Exception:
            pass
    except Exception as e:
        result['errors'].append(str(e))
    return result

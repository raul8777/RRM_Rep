import os, sys, json
from decimal import Decimal
from datetime import datetime, timezone
sys.path.insert(0, '/app')
import psycopg2

conn = psycopg2.connect(os.environ.get('DATABASE_URL', 'postgresql://crypto_agent:crypto_agent_password@postgres:5432/crypto_agent'))
cur = conn.cursor()

# Remove erroneous full close inserted by accidental close_position
cur.execute('DELETE FROM trades_closed WHERE id = %s', (890,))
print('deleted erroneous close id=890, rows', cur.rowcount)

metadata = {
    '_meta_updated_at': '2026-07-15T21:18:38.207228+00:00',
    '_meta_version': 15,
    'binance_entry_confirmed': True,
    'binance_validated_at': '2026-07-15T21:18:24.660740+00:00',
    'binance_validated_qty': 454.9,
    'btc_price_at_entry': 65000.0,
    'config_snapshot_id': '20260715_201730_774841',
    'entry_avg_price': 0.3528,
    'entry_commission': 0.00111574,
    'entry_filled_qty': 2442.2,
    'entry_order_id': '1000049550',
    'entry_pred_pnl': 0.15220038682636516,
    'entry_pred_prob': 0.5126,
    'entry_profile_label': 'A',
    'entry_regime': 'SPOT_BULL',
    'entry_type': 'normal',
    'es_escalonamiento': False,
    'escalonamiento_index': 1,
    'indicators_at_entry': {
        'ema': {'value': 0.32730999999999993},
        'rsi': {'value': 68.69781290547292},
        'sma': {'value': 0.32730999999999993},
        'macd': {'macd': 0.006571751173162166, 'signal': 0.0045540396456033, 'histogram': 0.0020177115275588657},
        'bollinger_bands': {'lower': 0.30894711351666077, 'upper': 0.3456728864833391, 'middle': 0.32730999999999993}
    },
    'last_peak_price': 0.3678,
    'order_id': 1000049550,
    'override_used': False,
    'partial_close': True,
    'partial_close_time': '2026-07-15T21:18:38.222966+00:00',
    'partial_qty_closed': 1987.2,
    'partial_qty_remaining': 454.9,
    'peak_pnl_pct_real': 0.03957,
    'peak_reconstruction_ts': '2026-07-15T21:18:36.883533+00:00',
    'pnl_prediction': {
        'symbol': 'ONDOUSDT',
        'trade_side': 'LONG',
        'price_range': {'max': 0.37149000000000004, 'min': 0.33610999999999996, 'volatility': 0.05190188378558273, 'adjusted_volatility': 0.05},
        'expected_pnl': 0.15220038682636516,
        'risk_metrics': {'max_gain_usdt': 29.26930515891673, 'max_loss_usdt': 29.26930515891673, 'percentage_risk': 5.000000000000011, 'percentage_reward': 5.000000000000011, 'risk_reward_ratio': 1.0},
        'current_price': 0.3538,
        'bootstrap_mode': True,
        'indicators_used': {
            'ema': {'value': 0.32730999999999993},
            'rsi': {'value': 68.69781290547292},
            'sma': {'value': 0.32730999999999993},
            'macd': {'macd': 0.006571751173162166, 'signal': 0.0045540396456033, 'histogram': 0.0020177115275588657},
            'bollinger_bands': {'lower': 0.30894711351666077, 'upper': 0.3456728864833391, 'middle': 0.32730999999999993}
        },
        'prediction_time': '2026-07-15T20:24:14.775156',
        'position_size_usdt': 585.3861031783333,
        'success_probability': 0.5126,
        'cycle_duration_minutes': 240,
        'expected_pnl_percentage': 0.025999999999999742
    },
    'profile_at_entry': 'A',
    'profile_label': 'A',
    'profit_realization_started_at': '2026-07-15T20:35:08.027351+00:00',
    'profit_realization_started_pnl_pct': 0.007409,
    'reevaluation_count': 0,
    'regime_at_entry': 'SPOT_BULL',
    'regime_mode': 'SPOT_BULL',
    'restored_after_sso_partial_close_gap': True,
    'rsi_at_entry': 68.69781290547292,
    'scale_out_done': True,
    'signal_id': None,
    'spot_total_at_open': 2729.74227988,
    'sso_last_reason': 'sso_multilevel_l1',
    'sso_last_trigger': 'SSO-3',
    'sso_levels_done': [1],
    'sso_pnl_snapshot': {'ts': '2026-07-15T20:35:08.010481+00:00', 'pnl_pct': 0.007914075749010653},
    'strength': 0.7788333333333335,
    'total_escalonamientos': 1,
    'trade_type': 'spot',
    'unified_governance_last_decision': {'action': 'hold', 'reason': 'unified_governance_profit_hold', 'regime': 'SPOT_BULL', 'profile': 'A', 'updated_at': '2026-07-15T20:45:50.817519+00:00', 'trade_state': 'profit', 'fng_modifier': 0.85},
    'vol_spike_at_entry': 5.28
}

pnl_pred = metadata['pnl_prediction']
indic = metadata['indicators_at_entry']

# Restore trades_open with current quantity 454.9 and updated TP floor
cur.execute(
    '''INSERT INTO trades_open (id, symbol, side, quantity, price, strategy_id, entry_time, stop_loss, take_profit, status, metadata, entry_commission, pnl_prediction, indicators_at_entry, corrupt)
       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
    (1055, 'ONDOUSDT', 'BUY', Decimal('454.9'), Decimal('0.3538'), None, datetime(2026,7,15,20,24,56,241881,tzinfo=timezone.utc), Decimal('0.3538'), Decimal('0.378566'), 'OPEN', json.dumps(metadata), Decimal('0.00111574'), json.dumps(pnl_pred), json.dumps(indic), False)
)
print('restored trades_open id=1055')

# Insert two partial closes using consistent PnL formula with trades_open.price=0.3538
entry_price = Decimal('0.3538')
entry_comm_total = Decimal('0.00111574')
full_qty = Decimal('2442.2')

partials = [
    {'qty': Decimal('1831.6'), 'exit_price': Decimal(str((132.9615 + 294.5052 + 207.02196 + 28.17643) / 1831.6)), 'exit_time': datetime(2026,7,15,21,8,2,tzinfo=timezone.utc), 'reason': 'sso_partial_close', 'trigger': 'SSO-2', 'trigger_reason': 'sso_momentum_exhausted'},
    {'qty': Decimal('155.6'), 'exit_price': Decimal('0.3641'), 'exit_time': datetime(2026,7,15,21,18,38,tzinfo=timezone.utc), 'reason': 'sso_partial_close', 'trigger': 'SSO-3', 'trigger_reason': 'sso_multilevel_l1'},
]

for p in partials:
    qty = p['qty']
    exit_p = p['exit_price']
    gross = (exit_p - entry_price) * qty
    entry_comm = entry_comm_total * (qty / full_qty)
    exit_comm = qty * exit_p * Decimal('0.0005')
    pnl = gross - entry_comm - exit_comm
    pnl_pct = (pnl / (qty * entry_price)) * Decimal('100') if qty * entry_price > 0 else Decimal('0')
    _meta = {
        'partial_close': True,
        'sso_trigger': p['trigger'],
        'sso_reason': p['trigger_reason'],
        'position_id': 1055,
        'source': 'manual_reconstruction_after_sso_gap'
    }
    print(f"inserting partial qty={qty} exit={exit_p} gross={gross} entry_comm={entry_comm} exit_comm={exit_comm} pnl={pnl} pnl_pct={pnl_pct}")
    cur.execute(
        '''INSERT INTO trades_closed (symbol, side, quantity, entry_price, exit_price, strategy_id, entry_time, exit_time, pnl, pnl_percentage, close_reason, metadata, entry_commission, exit_commission, pnl_prediction, indicators_at_entry, prediction_accuracy, is_corrupted, data_version, is_synthetic, learning_eligible, corrupt)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        ('ONDOUSDT', 'BUY', qty, entry_price, exit_p, None, datetime(2026,7,15,20,24,56,241881,tzinfo=timezone.utc), p['exit_time'], pnl, pnl_pct, p['reason'], json.dumps(_meta), entry_comm, exit_comm, json.dumps(pnl_pred), json.dumps(indic), None, False, 1, False, False, False)
    )

# Add audit record
cur.execute(
    'INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s,%s,%s,%s,%s)',
    ('ONDOUSDT', 'spot', 'sso_partial_close_reconstructed', json.dumps({'position_id': 1055, 'closed_qty': 1987.2, 'remaining_qty': 454.9, 'reason': 'Restored missing trades_closed rows after SSO partial closes'}), True)
)

conn.commit()
conn.close()
print('done')

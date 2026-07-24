"""
tests/test_usd_loss_guard.py
Unit tests for USD-denominated loss guardrail helpers.
"""

import sys
import os
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from run_trades import _apply_usd_loss_cap_to_sl


def _settings(max_loss_usd_per_trade=18.0):
    return types.SimpleNamespace(max_loss_usd_per_trade=max_loss_usd_per_trade)


def test_buy_sl_tightened_by_usd_cap():
    entry = 100.0
    qty = 1.0
    # category SL 20% below entry -> $20 loss (over $18 cap)
    sl_price = entry * 0.80
    tp_price = entry * 1.10
    metadata = {'sl_source': 'category_pct'}
    effective_sl, effective_tp, meta = _apply_usd_loss_cap_to_sl(
        'BTCUSDT', qty, entry, sl_price, tp_price, _settings(), 'BUY', metadata, 'spot'
    )
    # $18 cap -> SL $82
    assert effective_sl == 82.0
    assert meta['sl_source'] == 'usd_cap'
    assert meta['max_loss_usd_per_trade'] == 18.0
    assert 'effective_sl_price' in meta
    assert 'effective_tp_price' in meta
    assert effective_tp is not None and effective_tp > entry


def test_buy_already_tighter_sl_unchanged():
    entry = 100.0
    qty = 1.0
    sl_price = entry * 0.90  # $10 loss, tighter than $18 cap
    tp_price = entry * 1.10
    metadata = {'sl_source': 'category_pct'}
    effective_sl, effective_tp, meta = _apply_usd_loss_cap_to_sl(
        'BTCUSDT', qty, entry, sl_price, tp_price, _settings(), 'BUY', metadata, 'spot'
    )
    assert effective_sl == 90.0
    assert meta['sl_source'] == 'category_pct'


def test_sell_sl_tightened_by_usd_cap():
    entry = 100.0
    qty = 1.0
    sl_price = entry * 1.25  # $25 loss (over $18 cap)
    tp_price = entry * 0.90
    metadata = {'sl_source': 'category_pct'}
    effective_sl, effective_tp, meta = _apply_usd_loss_cap_to_sl(
        'BTCUSDT', qty, entry, sl_price, tp_price, _settings(), 'SELL', metadata, 'futures'
    )
    assert effective_sl == 118.0
    assert meta['sl_source'] == 'usd_cap'
    assert effective_tp is not None and effective_tp < entry


def test_zero_max_loss_returns_original():
    entry = 100.0
    qty = 1.0
    sl_price = entry * 0.95
    metadata = {}
    effective_sl, effective_tp, meta = _apply_usd_loss_cap_to_sl(
        'BTCUSDT', qty, entry, sl_price, 110.0, _settings(max_loss_usd_per_trade=0.0),
        'BUY', metadata, 'spot'
    )
    assert effective_sl == sl_price
    assert effective_tp == 110.0


def test_invalid_side_returns_original():
    entry = 100.0
    qty = 1.0
    sl_price = 90.0
    metadata = {}
    effective_sl, effective_tp, meta = _apply_usd_loss_cap_to_sl(
        'BTCUSDT', qty, entry, sl_price, 110.0, _settings(), 'UNKNOWN', metadata, 'spot'
    )
    assert effective_sl == sl_price
    assert effective_tp == 110.0

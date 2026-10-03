"""Tests for the thesis-anchored S/R path lifecycle (sr_path_v1).

Covers the full contract chain:
- support_resistance.build_sr_future_path (ordered forward map, both sides)
- position_thesis.build_sr_path_v1 (expected_candles = distance / atr, no fabrication)
- position_thesis.evaluate_sr_path_horizon (band logic, never blocks on missing geometry)
- position_intelligence.should_evaluate_thesis_path (candle-bucket gating)
- position_intelligence.evaluate_thesis_path (all 5 states)
- position_belief consumption (structural evidence only in active mode)
"""

from support_resistance import build_sr_future_path
from position_thesis import build_sr_path_v1, evaluate_sr_path_horizon
from position_intelligence import (
    evaluate_thesis_path,
    should_evaluate_thesis_path,
)


# ── build_sr_future_path ────────────────────────────────────────────────────

def test_future_path_long_orders_levels_forward():
    supports = [(90.0, 2.0), (95.0, 3.0), (85.0, 1.0)]
    resistances = [(105.0, 2.5), (110.0, 1.5), (120.0, 1.0)]
    path = build_sr_future_path(100.0, supports, resistances, "LONG")
    assert path["side"] == "LONG"
    assert path["anchor"] == 95.0          # nearest support below
    assert path["target"] == 105.0         # nearest resistance above
    assert path["next_levels"] == [110.0, 120.0]
    assert path["anchor_strength"] == 3.0
    assert path["target_strength"] == 2.5


def test_future_path_short_mirrors():
    supports = [(90.0, 2.0), (95.0, 3.0)]
    resistances = [(105.0, 2.5), (110.0, 1.5), (120.0, 1.0)]
    path = build_sr_future_path(100.0, supports, resistances, "SHORT")
    assert path["side"] == "SHORT"
    assert path["anchor"] == 105.0         # nearest resistance above (invalidation)
    assert path["target"] == 95.0          # nearest support below (objective)
    assert path["next_levels"] == [90.0]


def test_future_path_no_geometry_returns_empty_not_exception():
    path = build_sr_future_path(0.0, [], [], "LONG")
    assert path["anchor"] is None
    assert path["target"] is None
    assert path["next_levels"] == []


# ── build_sr_path_v1 ────────────────────────────────────────────────────────

def test_build_sr_path_v1_computes_expected_candles_from_atr():
    meta = {"current_price": 100.0}
    ind = {
        "sr_path_long": {
            "side": "LONG", "anchor": 95.0, "target": 110.0,
            "next_levels": [120.0], "anchor_strength": 2.0, "target_strength": 1.5,
        },
        "atr": 5.0,  # 5 price units per candle
        "interval": "1h",
    }
    path = build_sr_path_v1(
        side="LONG", metadata=meta, indicators_at_entry=ind,
        support_level=95.0, resistance_level=110.0,
        target_price=110.0, stop_price=95.0,
    )
    assert path is not None
    assert path["anchor_level"] == 95.0
    assert path["target_level"] == 110.0
    assert path["next_levels"] == [120.0]
    # distance = |110 - 100| = 10; atr = 5 → 2 candles
    assert path["expected_candles"] == 2.0
    assert path["primary_timeframe"] == "1h"


def test_build_sr_path_v1_falls_back_to_resolved_levels():
    meta = {"current_price": 100.0}
    path = build_sr_path_v1(
        side="SHORT", metadata=meta, indicators_at_entry={},
        support_level=90.0, resistance_level=108.0,
        target_price=90.0, stop_price=108.0,
    )
    assert path is not None
    assert path["side"] == "SHORT"
    assert path["anchor_level"] == 108.0   # resistance for SHORT
    assert path["target_level"] == 90.0
    # no ATR → no expected_candles (never fabricated)
    assert path["expected_candles"] is None


def test_build_sr_path_v1_returns_none_when_no_geometry():
    path = build_sr_path_v1(
        side="LONG", metadata={}, indicators_at_entry={},
        support_level=None, resistance_level=None,
        target_price=None, stop_price=None,
    )
    assert path is None


def test_build_sr_path_v1_converts_atr_pct_to_price_units():
    """atr_pct_at_entry is a percentage, not price units — expected_candles must
    be computed against price-unit ATR or sub-$1 assets break by 10000x."""
    meta = {"current_price": 0.001, "atr_pct_at_entry": 2.0}  # 2% of price per candle
    path = build_sr_path_v1(
        side="LONG", metadata=meta, indicators_at_entry={},
        support_level=0.0009, resistance_level=0.0011,
        target_price=0.0011, stop_price=0.0009,
    )
    assert path is not None
    # atr_candle = 2% * 0.001 = 0.00002; distance = 0.0001 → 5 candles
    assert path["atr_per_candle"] == 0.00002
    assert path["expected_candles"] == 5.0


# ── evaluate_sr_path_horizon ────────────────────────────────────────────────

def test_horizon_band_logic():
    assert evaluate_sr_path_horizon({"expected_candles": 0.5}, min_candles=1.0, max_candles=4.0)["reason"] == "below_min_horizon"
    assert evaluate_sr_path_horizon({"expected_candles": 2.5}, min_candles=1.0, max_candles=4.0)["in_band"] is True
    assert evaluate_sr_path_horizon({"expected_candles": 6.0}, min_candles=1.0, max_candles=4.0)["reason"] == "above_max_horizon"


def test_horizon_never_blocks_on_missing_geometry():
    assert evaluate_sr_path_horizon(None, min_candles=1.0, max_candles=4.0)["in_band"] is None
    assert evaluate_sr_path_horizon({"expected_candles": None}, min_candles=1.0, max_candles=4.0)["reason"] == "no_geometry"
    assert evaluate_sr_path_horizon({}, min_candles=1.0, max_candles=4.0)["in_band"] is None


# ── should_evaluate_thesis_path (candle-bucket gating) ─────────────────────

def test_candle_gating_only_fires_on_new_bucket():
    meta = {}
    # 1h candles: bucket advances once per hour
    assert should_evaluate_thesis_path(meta, "1h", now_ts=3600.0 * 10.5) is True
    meta["sr_path_last_eval_bucket"] = int(3600.0 * 10.5 // 3600.0)
    assert should_evaluate_thesis_path(meta, "1h", now_ts=3600.0 * 10.8) is False  # same bucket
    assert should_evaluate_thesis_path(meta, "1h", now_ts=3600.0 * 11.1) is True   # next bucket


def test_candle_gating_unknown_timeframe_never_blocks():
    assert should_evaluate_thesis_path({"sr_path_last_eval_bucket": 5}, None, now_ts=100.0) is True
    assert should_evaluate_thesis_path({"sr_path_last_eval_bucket": 5}, "", now_ts=100.0) is True


# ── evaluate_thesis_path (5 states) ─────────────────────────────────────────

def _path(side="LONG", anchor=95.0, target=110.0, next_levels=None, expected=2.0, entry=100.0):
    return {
        "side": side, "anchor_level": anchor, "target_level": target,
        "next_levels": next_levels or [], "expected_candles": expected,
        "entry_price": entry, "primary_timeframe": "1h",
    }


def test_path_invalidated_at_candle_close_beyond_anchor():
    res = evaluate_thesis_path(
        thesis_path=_path(), entry_price=100.0, current_price=94.0,
        sr_behavior={}, candles_consumed=1.0, vol_spike=1.0, settings=None,
    )
    assert res["state"] == "invalidated"
    assert res["reason"] == "anchor_broken_at_candle_close"


def test_path_target_reached():
    res = evaluate_thesis_path(
        thesis_path=_path(), entry_price=100.0, current_price=111.0,
        sr_behavior={}, candles_consumed=1.5, vol_spike=0.5, settings=None,
    )
    assert res["state"] == "target_reached"  # vol below ext threshold → no extension


def test_path_extends_to_next_level_with_volume():
    res = evaluate_thesis_path(
        thesis_path=_path(next_levels=[120.0]), entry_price=100.0, current_price=111.0,
        sr_behavior={}, candles_consumed=1.5, vol_spike=2.0, settings=None,
    )
    assert res["state"] == "extended_to_next_level"
    assert res["target_extended_to"] == 120.0


def test_path_stalled_on_candle_overrun_without_progress():
    res = evaluate_thesis_path(
        thesis_path=_path(expected=2.0), entry_price=100.0, current_price=101.0,
        sr_behavior={}, candles_consumed=5.0, vol_spike=1.0, settings=None,
    )
    # 5.0 >= 2.0 * 2.0 (default stall multiplier) and progress << 50%
    assert res["state"] == "stalled"
    assert res["reason"] == "no_progress_within_expected_path"


def test_path_stalled_on_anchor_absorption():
    res = evaluate_thesis_path(
        thesis_path=_path(), entry_price=100.0, current_price=97.0,
        sr_behavior={"support_tests": 3, "support_held": False},
        candles_consumed=1.0, vol_spike=1.0, settings=None,
    )
    assert res["state"] == "stalled"
    assert res["reason"] == "anchor_absorption_without_bounce"


def test_path_on_track():
    res = evaluate_thesis_path(
        thesis_path=_path(), entry_price=100.0, current_price=104.0,
        sr_behavior={"support_tests": 1, "support_held": True},
        candles_consumed=1.0, vol_spike=1.2, settings=None,
    )
    assert res["state"] == "on_track"


def test_path_short_side_mirror():
    res = evaluate_thesis_path(
        thesis_path=_path(side="SHORT", anchor=105.0, target=90.0, entry=100.0),
        entry_price=100.0, current_price=106.0,  # above anchor → invalidated for SHORT
        sr_behavior={}, candles_consumed=1.0, vol_spike=1.0, settings=None,
    )
    assert res["state"] == "invalidated"


def test_path_no_path_never_raises():
    res = evaluate_thesis_path(
        thesis_path=None, entry_price=100.0, current_price=101.0,
        sr_behavior={}, candles_consumed=0.0,
    )
    assert res["state"] == "no_path"


# ── belief consumption gated by mode ────────────────────────────────────────

def _thesis(side="LONG"):
    return {
        "side": side,
        "stop_price": 95.0 if side == "LONG" else 105.0,
        "target_price": 110.0 if side == "LONG" else 90.0,
        "entry_success_probability": 0.60,
    }


def test_belief_ignores_shadow_path_states():
    from position_belief import evaluate_belief
    kwargs = dict(
        side="LONG", pnl_usdt=-1.0, current_price=99.0,
        thesis_snapshot=_thesis("LONG"),
    )
    shadow = evaluate_belief(**kwargs, sr_path_state={"state": "invalidated", "mode": "shadow"})
    assert "sr_path_invalidated" not in shadow["reasons"]


def test_belief_consumes_active_path_states():
    from position_belief import evaluate_belief
    kwargs = dict(
        side="LONG", pnl_usdt=-1.0, current_price=99.0,
        thesis_snapshot=_thesis("LONG"),
    )
    active = evaluate_belief(**kwargs, sr_path_state={"state": "invalidated", "mode": "active"})
    assert "sr_path_invalidated" in active["reasons"]


def test_belief_favors_on_track_in_active_mode():
    from position_belief import evaluate_belief
    kwargs = dict(
        side="LONG", pnl_usdt=2.0, current_price=104.0,
        thesis_snapshot=_thesis("LONG"),
    )
    res = evaluate_belief(**kwargs, sr_path_state={"state": "on_track", "mode": "active"})
    assert "sr_path_on_track" in res["reasons"]

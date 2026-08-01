"""Tests for position_intelligence.py — PIM evaluator, behavioral tracker, S/R cache."""
import time
from unittest.mock import patch

import pytest

from position_intelligence import (
    PIMAction,
    compute_sr_aware_trail_distance,
    evaluate_position_intelligence,
    has_behavior_changed,
    invalidate_sr_cache,
    refresh_sr_levels,
    update_sr_behavior,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_sr_levels(
    support_level=100.0,
    resistance_level=120.0,
    zone=0.5,
    bounce=False,
    breakout=False,
    breakdown=False,
    support_strength=0.6,
    atr_pct=2.0,
):
    return {
        "support_level": support_level,
        "resistance_level": resistance_level,
        "sr_zone_position": zone,
        "sr_bounce": bounce,
        "sr_breakout": breakout,
        "sr_breakdown": breakdown,
        "support_strength": support_strength,
        "atr_pct": atr_pct,
        "nearest_support_dist_pct": None,
        "nearest_resistance_dist_pct": None,
    }


def _make_behavior(
    support_tests=0,
    support_held=None,
    resistance_tests=0,
    resistance_broken=None,
    momentum_dir=0.0,
    ticks_total=100,
    ticks_profit=50,
):
    return {
        "support_tests": support_tests,
        "support_held": support_held,
        "resistance_tests": resistance_tests,
        "resistance_broken": resistance_broken,
        "momentum_direction": momentum_dir,
        "ticks_total": ticks_total,
        "ticks_in_profit_zone": ticks_profit,
        "momentum_samples": [],
        "max_zone_reached": 0.8,
        "min_zone_reached": 0.2,
        "last_sr_event": None,
        "last_sr_event_at": None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# EMERGENCY TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestEmergency:
    """Test EMERGENCY decisions on confirmed structural breaks."""

    def test_long_breakdown_high_confidence(self):
        """Long + breakdown + support failed before + losing = emergency."""
        sr = _make_sr_levels(breakdown=True, zone=0.1)
        beh = _make_behavior(support_tests=2, support_held=False, momentum_dir=-0.002)
        result = evaluate_position_intelligence(
            entry_price=110.0, current_price=100.0, side="LONG",
            sr_levels=sr, sr_behavior=beh,
        )
        assert result.action == PIMAction.EMERGENCY
        assert result.confidence >= 0.7
        assert "support_broken" in result.reason

    def test_long_breakdown_low_confidence_no_emergency(self):
        """Long + breakdown but support never tested before = below threshold."""
        sr = _make_sr_levels(breakdown=True, zone=0.1)
        beh = _make_behavior(support_tests=0, support_held=None, momentum_dir=0.001)
        result = evaluate_position_intelligence(
            entry_price=100.0, current_price=105.0, side="LONG",
            sr_levels=sr, sr_behavior=beh,
        )
        # confidence = 0.6 base, no additional boosters, in profit reduces 0.1
        # Result should NOT be emergency (confidence < 0.7)
        assert result.confidence < 0.7 or result.action != PIMAction.EMERGENCY

    def test_short_breakout_emergency(self):
        """Short + resistance broken + momentum up + losing = emergency."""
        sr = _make_sr_levels(breakout=True, zone=0.9)
        beh = _make_behavior(resistance_tests=2, resistance_broken=True, momentum_dir=0.003)
        result = evaluate_position_intelligence(
            entry_price=100.0, current_price=110.0, side="SHORT",
            sr_levels=sr, sr_behavior=beh,
        )
        assert result.action == PIMAction.EMERGENCY
        assert result.confidence >= 0.7


# ══════════════════════════════════════════════════════════════════════════════
# HOLD TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestHold:
    """Test HOLD decisions at favorable levels with evidence."""

    def test_long_at_support_with_bounce_holds(self):
        """Long in loss at support zone with bounce confirmation = HOLD."""
        sr = _make_sr_levels(zone=0.15, bounce=True, support_strength=0.8)
        beh = _make_behavior(support_tests=1, support_held=True, momentum_dir=0.001)
        result = evaluate_position_intelligence(
            entry_price=110.0, current_price=102.0, side="LONG",
            sr_levels=sr, sr_behavior=beh,
        )
        assert result.action == PIMAction.HOLD
        assert "support_hold" in result.reason
        assert result.confidence >= 0.5

    def test_long_at_support_but_previously_failed(self):
        """Long in loss at support but support failed before = NO HOLD."""
        sr = _make_sr_levels(zone=0.15, bounce=False, support_strength=0.4)
        beh = _make_behavior(support_tests=3, support_held=False, momentum_dir=-0.003)
        result = evaluate_position_intelligence(
            entry_price=110.0, current_price=102.0, side="LONG",
            sr_levels=sr, sr_behavior=beh,
        )
        # Should NOT be a hold because counter-evidence too strong
        assert result.action != PIMAction.HOLD or result.confidence < 0.5

    def test_short_at_resistance_holds(self):
        """Short in loss at resistance zone without breakout = HOLD."""
        sr = _make_sr_levels(zone=0.85, breakout=False)
        beh = _make_behavior(resistance_tests=1, resistance_broken=False, momentum_dir=-0.001)
        result = evaluate_position_intelligence(
            entry_price=100.0, current_price=108.0, side="SHORT",
            sr_levels=sr, sr_behavior=beh,
        )
        assert result.action == PIMAction.HOLD
        assert result.confidence >= 0.5

    def test_long_in_profit_not_at_support_neutral(self):
        """Long in profit in middle of range = neutral (no hold needed)."""
        sr = _make_sr_levels(zone=0.5)
        beh = _make_behavior()
        result = evaluate_position_intelligence(
            entry_price=100.0, current_price=110.0, side="LONG",
            sr_levels=sr, sr_behavior=beh,
        )
        assert result.reason == "neutral"
        assert result.confidence == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# TIGHTEN TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestTighten:
    """Test TIGHTEN decisions when approaching opposing level with profit."""

    def test_long_profit_approaching_resistance_tightens(self):
        """Long in profit near resistance, previously rejected = TIGHTEN."""
        sr = _make_sr_levels(zone=0.85, resistance_level=120.0)
        beh = _make_behavior(resistance_tests=1, resistance_broken=False, momentum_dir=-0.0005)
        result = evaluate_position_intelligence(
            entry_price=100.0, current_price=118.0, side="LONG",
            sr_levels=sr, sr_behavior=beh,
        )
        assert result.action == PIMAction.TIGHTEN
        assert result.confidence >= 0.5

    def test_long_profit_breakout_does_not_tighten(self):
        """Long in profit with confirmed breakout = HOLD (let it run)."""
        sr = _make_sr_levels(zone=0.85, breakout=True)
        beh = _make_behavior(resistance_tests=1, resistance_broken=True)
        result = evaluate_position_intelligence(
            entry_price=100.0, current_price=118.0, side="LONG",
            sr_levels=sr, sr_behavior=beh,
        )
        assert result.action == PIMAction.HOLD
        assert "breakout_momentum" in result.reason

    def test_short_profit_approaching_support_tightens(self):
        """Short in profit near support, previously held = TIGHTEN."""
        sr = _make_sr_levels(zone=0.15, support_level=100.0, breakdown=False)
        beh = _make_behavior(support_tests=2, support_held=True, momentum_dir=0.0005)
        result = evaluate_position_intelligence(
            entry_price=120.0, current_price=101.0, side="SHORT",
            sr_levels=sr, sr_behavior=beh,
        )
        assert result.action == PIMAction.TIGHTEN
        assert result.confidence >= 0.5


# ══════════════════════════════════════════════════════════════════════════════
# FLAG_EXIT TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestFlagExit:
    """Test FLAG_EXIT decisions when trade thesis is exhausted."""

    def test_long_multiple_rejections_flags_exit(self):
        """Long in profit but resistance rejected 3x = FLAG_EXIT."""
        sr = _make_sr_levels(zone=0.6, resistance_level=120.0, atr_pct=2.0)
        beh = _make_behavior(resistance_tests=3, resistance_broken=False)
        result = evaluate_position_intelligence(
            entry_price=100.0, current_price=115.0, side="LONG",
            sr_levels=sr, sr_behavior=beh,
            peak_pnl_pct=0.18,  # Had 18% peak, now at 15% = giveback
        )
        assert result.action == PIMAction.FLAG_EXIT
        assert "exhaustion" in result.reason
        assert result.confidence >= 0.5

    def test_short_multiple_bounces_flags_exit(self):
        """Short in profit but support bounced 2x = FLAG_EXIT."""
        sr = _make_sr_levels(zone=0.4, support_level=95.0, atr_pct=2.0)
        beh = _make_behavior(support_tests=2, support_held=True)
        result = evaluate_position_intelligence(
            entry_price=120.0, current_price=100.0, side="SHORT",
            sr_levels=sr, sr_behavior=beh,
            peak_pnl_pct=0.20,
        )
        assert result.action == PIMAction.FLAG_EXIT
        assert result.confidence >= 0.5

    def test_single_rejection_not_enough(self):
        """Only 1 rejection is not enough to flag exit."""
        sr = _make_sr_levels(zone=0.6, resistance_level=120.0)
        beh = _make_behavior(resistance_tests=1, resistance_broken=False)
        result = evaluate_position_intelligence(
            entry_price=100.0, current_price=115.0, side="LONG",
            sr_levels=sr, sr_behavior=beh,
        )
        assert result.action != PIMAction.FLAG_EXIT


# ══════════════════════════════════════════════════════════════════════════════
# BEHAVIORAL TRACKER TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestBehavioralTracker:
    """Test update_sr_behavior state transitions."""

    def test_first_call_initializes(self):
        """First call should initialize all counters."""
        sr = _make_sr_levels(support_level=100.0, resistance_level=120.0, zone=0.5, atr_pct=2.0)
        meta = {}
        result = update_sr_behavior(meta, 110.0, sr, "LONG")
        assert result['support_tests'] == 0
        assert result['resistance_tests'] == 0
        assert result['ticks_total'] == 1
        assert len(result['momentum_samples']) == 1

    def test_support_test_detected(self):
        """Price near support should increment support_tests."""
        sr = _make_sr_levels(support_level=100.0, resistance_level=120.0, atr_pct=2.0)
        meta = {'sr_behavior': {'support_tests': 0, 'resistance_tests': 0,
                                'ticks_total': 5, 'ticks_in_profit_zone': 2,
                                'max_zone_reached': 0.5, 'min_zone_reached': 0.3,
                                'momentum_samples': [105, 104, 103, 102, 101],
                                'support_held': None, 'resistance_broken': None,
                                'last_sr_event': None, 'last_sr_event_at': None,
                                '_at_support': False, '_at_resistance': False}}
        # Price very close to support (within cluster)
        result = update_sr_behavior(meta, 100.1, sr, "LONG")
        assert result['support_tests'] == 1
        assert result['_at_support'] is True

    def test_support_held_on_leaving(self):
        """After being at support, leaving above = support_held=True."""
        sr = _make_sr_levels(support_level=100.0, resistance_level=120.0, atr_pct=2.0)
        meta = {'sr_behavior': {'support_tests': 1, 'resistance_tests': 0,
                                'ticks_total': 10, 'ticks_in_profit_zone': 3,
                                'max_zone_reached': 0.5, 'min_zone_reached': 0.1,
                                'momentum_samples': [105, 103, 101, 100, 100.1],
                                'support_held': None, 'resistance_broken': None,
                                'last_sr_event': None, 'last_sr_event_at': None,
                                '_at_support': True, '_at_resistance': False}}
        # Price moves away from support (above it)
        result = update_sr_behavior(meta, 105.0, sr, "LONG")
        assert result['_at_support'] is False
        assert result['support_held'] is True
        assert result['last_sr_event'] == 'bounce'

    def test_momentum_direction_computed(self):
        """Momentum direction should be positive when price rising."""
        sr = _make_sr_levels(support_level=90.0, resistance_level=120.0, atr_pct=2.0)
        meta = {'sr_behavior': {'support_tests': 0, 'resistance_tests': 0,
                                'ticks_total': 10, 'ticks_in_profit_zone': 5,
                                'max_zone_reached': 0.5, 'min_zone_reached': 0.3,
                                'momentum_samples': [100, 101, 102, 103, 104, 105, 106, 107, 108, 109],
                                'support_held': None, 'resistance_broken': None,
                                'last_sr_event': None, 'last_sr_event_at': None,
                                '_at_support': False, '_at_resistance': False}}
        result = update_sr_behavior(meta, 110.0, sr, "LONG")
        assert result['momentum_direction'] > 0

    def test_has_behavior_changed_detects_new_test(self):
        """has_behavior_changed should return True when support_tests increments."""
        old = _make_behavior(support_tests=1)
        new = _make_behavior(support_tests=2)
        assert has_behavior_changed(old, new) is True

    def test_has_behavior_changed_false_when_same(self):
        """has_behavior_changed should return False when nothing important changed."""
        old = _make_behavior(support_tests=1, momentum_dir=0.001)
        new = _make_behavior(support_tests=1, momentum_dir=0.002)  # momentum changes don't trigger
        assert has_behavior_changed(old, new) is False


# ══════════════════════════════════════════════════════════════════════════════
# TRAILING DISTANCE TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestTrailingDistance:
    """Test compute_sr_aware_trail_distance adaptive trailing."""

    def test_far_from_resistance_uses_base(self):
        """When far from resistance, trailing distance = base."""
        sr = _make_sr_levels(resistance_level=130.0)
        dist = compute_sr_aware_trail_distance("LONG", 0.015, sr, 100.0)
        # 30% away from resistance, base = 1.5% → factor should be ~1.0
        assert dist >= 0.014  # very close to base

    def test_close_to_resistance_tightens(self):
        """When very close to resistance, trailing distance reduces."""
        sr = _make_sr_levels(resistance_level=101.0)
        dist = compute_sr_aware_trail_distance("LONG", 0.015, sr, 100.0)
        # Only 1% from resistance, base = 1.5% → factor = 1%/1.5% = 0.67
        assert dist < 0.015
        assert dist >= 0.015 * 0.3  # floor at 30%

    def test_floor_at_30_percent(self):
        """Trailing distance never goes below 30% of base."""
        sr = _make_sr_levels(resistance_level=100.01)  # Almost at resistance
        dist = compute_sr_aware_trail_distance("LONG", 0.015, sr, 100.0)
        assert dist >= 0.015 * 0.29  # ~30% floor (allow small float imprecision)

    def test_short_near_support_tightens(self):
        """Short position near support should tighten trailing."""
        sr = _make_sr_levels(support_level=99.0)
        dist = compute_sr_aware_trail_distance("SHORT", 0.015, sr, 100.0)
        # 1% from support, base = 1.5% → factor = 1%/1.5% = 0.67
        assert dist < 0.015

    def test_no_sr_levels_returns_base(self):
        """When no S/R data available, use base trailing distance."""
        dist = compute_sr_aware_trail_distance("LONG", 0.015, None, 100.0)
        assert dist == 0.015

    def test_no_opposing_level_returns_base(self):
        """When no opposing level (no resistance for long), use base."""
        sr = _make_sr_levels(resistance_level=None, support_level=90.0)
        dist = compute_sr_aware_trail_distance("LONG", 0.015, sr, 100.0)
        assert dist == 0.015


# ══════════════════════════════════════════════════════════════════════════════
# S/R CACHE TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestSRCache:
    """Test refresh_sr_levels caching behavior."""

    def test_cache_returns_fresh_data_without_refetch(self):
        """Second call within refresh interval should use cache."""
        call_count = [0]

        def mock_fetch(symbol, interval, limit):
            call_count[0] += 1
            # Return minimal valid klines (150 bars)
            return [[0, "100", "102", "98", str(100 + i * 0.1), "1000", 0] for i in range(150)]

        invalidate_sr_cache("TESTUSDT")
        result1 = refresh_sr_levels("TESTUSDT", mock_fetch, 110.0)
        result2 = refresh_sr_levels("TESTUSDT", mock_fetch, 110.0)
        assert call_count[0] == 1  # Only fetched once
        assert result1 is not None
        assert result2 is not None

    def test_cache_invalidation(self):
        """After invalidation, next call should re-fetch."""
        call_count = [0]

        def mock_fetch(symbol, interval, limit):
            call_count[0] += 1
            return [[0, "100", "102", "98", str(100 + i * 0.1), "1000", 0] for i in range(150)]

        invalidate_sr_cache("TESTUSDT2")
        refresh_sr_levels("TESTUSDT2", mock_fetch, 110.0)
        assert call_count[0] == 1
        invalidate_sr_cache("TESTUSDT2")
        refresh_sr_levels("TESTUSDT2", mock_fetch, 110.0)
        assert call_count[0] == 2

    def test_insufficient_klines_returns_none(self):
        """Should return None when not enough klines are available."""
        def mock_fetch(symbol, interval, limit):
            return [[0, "100", "102", "98", "101", "1000", 0] for _ in range(30)]

        invalidate_sr_cache("SMALLUSDT")
        result = refresh_sr_levels("SMALLUSDT", mock_fetch, 100.0)
        assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# EDGE CASES
# ══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Test edge cases and defensive behavior."""

    def test_invalid_prices_returns_hold(self):
        """Invalid prices should return neutral hold."""
        sr = _make_sr_levels()
        beh = _make_behavior()
        result = evaluate_position_intelligence(
            entry_price=0.0, current_price=100.0, side="LONG",
            sr_levels=sr, sr_behavior=beh,
        )
        assert result.action == PIMAction.HOLD
        assert result.reason == "invalid_prices"

    def test_empty_sr_levels_graceful(self):
        """Empty/minimal S/R levels should not crash."""
        sr = {"sr_zone_position": 0.5, "support_level": None, "resistance_level": None,
              "sr_breakout": False, "sr_bounce": False, "sr_breakdown": False,
              "support_strength": 0.0, "atr_pct": 1.0}
        beh = _make_behavior()
        result = evaluate_position_intelligence(
            entry_price=100.0, current_price=105.0, side="LONG",
            sr_levels=sr, sr_behavior=beh,
        )
        assert result is not None
        assert result.action == PIMAction.HOLD

    def test_confidence_never_exceeds_1(self):
        """Confidence should always be clamped to [0, 1]."""
        # Max boosters scenario
        sr = _make_sr_levels(breakdown=True, zone=0.05, support_strength=1.0)
        beh = _make_behavior(support_tests=5, support_held=False, momentum_dir=-0.01)
        result = evaluate_position_intelligence(
            entry_price=110.0, current_price=95.0, side="LONG",
            sr_levels=sr, sr_behavior=beh,
        )
        assert result.confidence <= 1.0
        assert result.confidence >= 0.0

"""staged_entry.py — multi-cycle ready-state validation before opening positions.

Provides a persistent, auditable `ready` state between the entry-profile gate
/QUBO selection and actual trade execution.  Each re-check (intra- or inter-cycle)
is stored in `ready_candidate_checks` with the full feature snapshot so future
analysis can correlate validation decisions with realized PnL.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from config_helpers import get_param, get_staged_entry_regime_config
from directional_confirmation import evaluate_shadow as evaluate_directional_shadow
from entry_decision import evaluate as evaluate_entry_decision
from entry_profile_gate import _DEFAULT_THRESHOLDS_PATH as _ENTRY_PROFILE_THRESHOLDS_PATH

logger = logging.getLogger(__name__)

# Optional imports (kept lazy to avoid heavy startup)
try:
    import entry_profile_gate
except Exception:  # pragma: no cover
    entry_profile_gate = None  # type: ignore


def _to_native(obj: Any) -> Any:
    """Recursively convert numpy/pandas scalars to plain Python types."""
    if isinstance(obj, dict):
        return {k: _to_native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_native(v) for v in obj]
    if hasattr(obj, "item") and callable(getattr(obj, "item")):
        return obj.item()
    if isinstance(obj, (int, float, bool, str, type(None))):
        return obj
    return str(obj)


def _json(obj: Any) -> str:
    return json.dumps(_to_native(obj), default=str)


def _now() -> datetime:
    return datetime.now(timezone.utc)


_TERMINAL_STAGE_REASONS = {
    "MAX_AGE_EXPIRED": "TRIGGER_TIMEOUT",
    "MAX_CYCLES_REACHED": "TRIGGER_TIMEOUT",
    "DIRECTION_TARGET_MISSING": "TRIGGER_INVALID",
    "DIRECTION_TARGET_INVALID": "TRIGGER_INVALID",
    "DIRECTION_STOP_BREACHED": "TRIGGER_INVALID",
    "DIRECTION_DRAWDOWN_LIMIT": "TRIGGER_INVALID",
    "DIRECTION_SR_PROXIMITY_LOST": "TRIGGER_INVALID",
    "DUPLICATE_POSITION_SKIP": "EXECUTION_BLOCKED",
    "DIRECTION_NOT_CONFIRMED": "TRIGGER_TIMEOUT",
    "QUALITY_BELOW_C": "SETUP_INVALID",
}


def _seconds_since(iso_value: Optional[str]) -> Optional[float]:
    if not iso_value:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (_now() - dt).total_seconds())
    except Exception:
        return None


def _stage_payload(
    previous_data: Dict[str, Any],
    *,
    stage_kind: str,
    stage_reason: str,
    max_age_min: Optional[int] = None,
) -> Dict[str, Any]:
    previous_stage_at = None
    if isinstance(previous_data, dict):
        previous_stage_at = previous_data.get("stage_updated_at")
    payload: Dict[str, Any] = {
        "stage_kind": stage_kind,
        "stage_reason": stage_reason,
        "stage_updated_at": _now().isoformat(),
    }
    elapsed = _seconds_since(previous_stage_at)
    if elapsed is not None:
        payload["time_in_stage_sec"] = round(elapsed, 3)
    if max_age_min is not None and max_age_min > 0:
        payload["stage_ttl_min"] = int(max_age_min)
    return payload


def _terminal_stage_reason(reason: str) -> str:
    normalized = str(reason or "").strip().upper()
    if normalized in _TERMINAL_STAGE_REASONS:
        return _TERMINAL_STAGE_REASONS[normalized]
    if normalized.startswith("DIRECTION_"):
        return "TRIGGER_INVALID"
    if normalized.startswith("CAPITAL_ROUTING_"):
        return "ROUTING_BLOCKED"
    if normalized.startswith("RISK_"):
        return "RISK_BLOCKED"
    if normalized.startswith("EXECUTION_"):
        return "EXECUTION_BLOCKED"
    return "SETUP_INVALID"


def _minutes_since(ts: Optional[Any]) -> Optional[float]:
    if ts is None:
        return None
    try:
        if isinstance(ts, datetime):
            dt = ts
        else:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (_now() - dt).total_seconds() / 60.0)
    except Exception:
        return None


def _is_slot_or_capital_block_reason(reason: Optional[str], tokens: List[str]) -> bool:
    reason_norm = str(reason or "").strip().upper()
    if not reason_norm:
        return False
    for token in tokens:
        token_norm = str(token or "").strip().upper()
        if token_norm and token_norm in reason_norm:
            return True
    return False


def _candidate_has_slot_or_capital_block(data: Dict[str, Any], tokens: List[str]) -> bool:
    if not isinstance(data, dict):
        return False

    capital_routing = data.get("capital_routing") or {}
    if isinstance(capital_routing, dict):
        status = str(capital_routing.get("status") or "").strip().lower()
        retry_count = int(capital_routing.get("retry_count") or 0)
        reason = str(capital_routing.get("reason") or "")
        if status in {"blocked", "exhausted"} and retry_count > 0:
            return True
        if _is_slot_or_capital_block_reason(reason, tokens):
            return True

    last_gate = data.get("last_execution_gate") or {}
    if isinstance(last_gate, dict):
        gate_reason = str(last_gate.get("reason") or "")
        if _is_slot_or_capital_block_reason(gate_reason, tokens):
            return True

    stage_reason = str(data.get("stage_reason") or "")
    if _is_slot_or_capital_block_reason(stage_reason, tokens):
        return True

    return False


def _apply_stale_hold_policy(
    db_client: Any,
    row: Dict[str, Any],
    cfg: Dict[str, Any],
    settings: Any,
) -> bool:
    """Discard stale validated rows that are waiting without a slot/capital block reason.

    Policy:
      - hard TTL: always discard when exceeded.
      - soft TTL + require-block: after soft TTL, keep waiting only if there is
        evidence of slot/capital blocking in the latest execution gate trail.
    """
    if not bool(get_param(settings, "staged_hold_policy_enabled", True)):
        return False

    data = row.get("data") or {}
    expected_revision = int(data.get("candidate_revision", 0) or 0)
    age_base = row.get("validated_at") or row.get("ready_at")
    age_min = _minutes_since(age_base)
    if age_min is None:
        return False

    soft_ttl = float(get_param(settings, "staged_hold_soft_ttl_min", 60.0) or 0.0)
    hard_ttl = float(get_param(settings, "staged_hold_hard_ttl_min", 120.0) or 0.0)
    require_block = bool(get_param(settings, "staged_hold_require_block_reason", True))
    tokens = list(get_param(settings, "staged_hold_allowed_block_reason_tokens", [
        "CAPITAL_ROUTING",
        "INSUFFICIENT_USDT",
        "MAX_POSITIONS",
        "PER_SYMBOL_LIMIT",
        "EXPOSURE_CAP",
        "SLOT",
    ]) or [])

    discard_reason: Optional[str] = None
    if hard_ttl > 0 and age_min >= hard_ttl:
        discard_reason = "STALE_HOLD_HARD_TTL_EXPIRED"
    elif require_block and soft_ttl > 0 and age_min >= soft_ttl:
        blocked = _candidate_has_slot_or_capital_block(data, tokens)
        if not blocked:
            discard_reason = "STALE_HOLD_NO_SLOT_OR_CAPITAL_BLOCK"

    if not discard_reason:
        return False

    if _discard_ready(db_client, int(row["id"]), discard_reason, expected_revision=expected_revision):
        _record_check(
            db_client,
            int(row["id"]),
            None,
            _next_check_sequence(db_client, int(row["id"])),
            "lifecycle",
            None,
            None,
            None,
            0.0,
            {},
            None,
            {
                "age_min": round(age_min, 3),
                "soft_ttl_min": soft_ttl,
                "hard_ttl_min": hard_ttl,
                "require_block_reason": require_block,
                "stage_reason": data.get("stage_reason"),
                "last_execution_gate": data.get("last_execution_gate"),
            },
            False,
            "terminal_reject",
            discard_reason,
            0,
            writer="risk_selector",
        )
        return True
    return False


def _trade_type_for_regime(regime_mode: str) -> str:
    return "futures" if regime_mode == "FUTURES_SHORT" else "spot"


def _side_for_regime(regime_mode: str) -> str:
    return "SHORT" if regime_mode == "FUTURES_SHORT" else "LONG"


def _selected_mode_for_meta(meta: Dict[str, Any]) -> str:
    arb = meta.get("directional_arbitration") if isinstance(meta, dict) else None
    if isinstance(arb, dict):
        selected = str(arb.get("selected_mode") or "").strip().upper()
        if selected:
            return selected
    return str(meta.get("selected_mode") or "").strip().upper()


def _trade_type_for_selected_mode(selected_mode: str) -> Optional[str]:
    if selected_mode in {"FUTURES_SHORT", "FUTURES_LONG"}:
        return "futures"
    if selected_mode == "SPOT_LONG":
        return "spot"
    return None


def _side_for_selected_mode(selected_mode: str) -> Optional[str]:
    if selected_mode == "FUTURES_SHORT":
        return "SHORT"
    if selected_mode in {"FUTURES_LONG", "SPOT_LONG"}:
        return "LONG"
    return None


def _trade_type_for_meta(meta: Dict[str, Any], regime_mode: str) -> str:
    selected_mode = _selected_mode_for_meta(meta)
    selected_trade_type = _trade_type_for_selected_mode(selected_mode)
    if selected_trade_type:
        return selected_trade_type
    requested = str(meta.get("trade_type") or "").strip().lower()
    if requested in {"spot", "futures"}:
        return requested
    return _trade_type_for_regime(regime_mode)


def _side_for_meta(meta: Dict[str, Any], regime_mode: str, trade_type: Optional[str] = None) -> str:
    selected_mode = _selected_mode_for_meta(meta)
    selected_side = _side_for_selected_mode(selected_mode)
    if selected_side:
        return selected_side
    requested = str(meta.get("expected_side") or "").strip().upper()
    if requested in {"LONG", "SHORT"}:
        return requested
    resolved_trade_type = trade_type or _trade_type_for_meta(meta, regime_mode)
    if resolved_trade_type == "futures" and str(regime_mode or "").upper() == "FUTURES_SHORT":
        return "SHORT"
    return _side_for_regime(regime_mode)


def _align_target_stop_with_side(
    base_price: Optional[float],
    target_price: Optional[float],
    stop_price: Optional[float],
    side: str,
) -> Tuple[Optional[float], Optional[float]]:
    """Swap target/stop when both are inverted for the directional side.

    This guards cases where policy values are LONG-oriented while the selected
    directional intent is SHORT (or vice versa).
    """
    base = _number(base_price)
    target = _number(target_price)
    stop = _number(stop_price)
    if base is None or target is None or stop is None:
        return target_price, stop_price

    is_short = str(side or "").upper() == "SHORT"
    short_inverted = target >= base and stop <= base
    long_inverted = target <= base and stop >= base
    if (is_short and short_inverted) or ((not is_short) and long_inverted):
        return stop, target
    return target_price, stop_price


def _is_sr_confirmed_signal(meta: Dict[str, Any]) -> bool:
    return bool(
        meta.get("support_bounce_buy") or meta.get("sr_bounce")
        or meta.get("support_breakout_buy") or meta.get("sr_breakout")
        or meta.get("support_breakdown_sell") or meta.get("sr_breakdown")
    )


def _adapt_cfg_for_sr_signal(cfg: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    """Return a less restrictive staged-entry config for confirmed SR setups."""
    if not _is_sr_confirmed_signal(meta):
        return cfg
    adapted = dict(cfg)
    adapted["required_positive_checks"] = min(int(adapted.get("required_positive_checks", 2) or 2), 1)
    adapted["direction_required_confirmations"] = min(int(adapted.get("direction_required_confirmations", 2) or 2), 1)
    adapted["direction_min_progress_bps"] = min(float(adapted.get("direction_min_progress_bps", 3.0) or 3.0), 1.0)
    adapted["max_cycles"] = max(int(adapted.get("max_cycles", 3) or 3), 4)
    return adapted


def _symbol_from_candidate(cand: Any) -> Optional[str]:
    if isinstance(cand, dict):
        return cand.get("symbol")
    if isinstance(cand, str):
        return cand
    return None


def _build_meta_for_symbol(
    symbol: str,
    signal_meta: Optional[Dict[str, Any]],
    predictions: Optional[Dict[str, Any]],
    candidate: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build a metadata dict merging signal, prediction and candidate keys."""
    meta: Dict[str, Any] = {}
    if isinstance(signal_meta, dict) and symbol in signal_meta:
        meta = dict(signal_meta.get(symbol, {}) or {})
    if isinstance(candidate, dict):
        meta.update(candidate)

    pred = (predictions or {}).get(symbol)
    if pred:
        meta.setdefault("entry_decision_prediction", pred)
        for key in ("expected_pnl", "expected_pnl_percentage", "position_size_usdt", "success_probability"):
            if pred.get(key) is not None:
                meta.setdefault(key, pred.get(key))
    return meta


def _get_fear_greed_value(news_client: Optional[Any]) -> Optional[float]:
    if news_client is None:
        return None
    try:
        data = news_client.get_fear_greed()
        if isinstance(data, dict):
            return float(data.get("value"))
    except Exception:
        pass
    return None


def _position_size_for_validation(meta: Dict[str, Any]) -> float:
    size = float(meta.get("position_size_usdt") or meta.get("expected_pnl_usdt") or 0.0)
    if size > 0:
        return size
    return 100.0


def _get_price(client: Any, symbol: str, price_source: str) -> Optional[float]:
    try:
        if price_source == "avg":
            return float(client.get_symbol_avg_price(symbol, use_cache=True))
        return float(client.get_symbol_price(symbol, use_cache=True))
    except Exception as exc:
        logger.warning(f"[StagedEntry] price fetch failed for {symbol} ({price_source}): {exc}")
        return None


def _classify_with_gate(
    meta: Dict[str, Any],
    regime_mode: str,
    symbol: str,
    settings: Any,
    fear_greed_value: Optional[float],
) -> Tuple[Optional[str], float, Dict[str, Any]]:
    """Classify the candidate and return (profile, quality_score, gate_info)."""
    if entry_profile_gate is None:
        return None, 0.0, {}
    try:
        thresholds_path = getattr(settings, "entry_profile_gate_thresholds_path", "Dev/benchmarks/entry_profile_calibration/entry_profile_thresholds.json")
        direction = meta.get("expected_side") or _side_for_regime(regime_mode)
        profile, info = entry_profile_gate.classify(
            meta,
            regime_mode,
            direction=direction,
            symbol=symbol,
            thresholds_path=thresholds_path,
            fear_greed_value=fear_greed_value,
            range_scalp_model_mode=getattr(settings, "range_scalp_quality_model_mode", "shadow"),
            range_scalp_model_path=getattr(settings, "range_scalp_quality_model_path", None),
        )
        return profile, float(info.get("score", 0.0)), info
    except Exception as exc:
        logger.warning(f"[StagedEntry] entry_profile_gate.classify failed for {symbol}: {exc}")
        return None, 0.0, {}


def _run_predictor_for_validation(
    client: Any,
    symbol: str,
    settings: Any,
    meta: Dict[str, Any],
    current_price: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Run pnl_service.predict_for_symbol using precomputed indicators and current price."""
    try:
        import pnl_service
        trade_type = meta.get("trade_type") or "spot"
        trade_side = meta.get("expected_side") or ("SHORT" if trade_type == "futures" else "LONG")
        position_size = _position_size_for_validation(meta)
        precomputed = {"indicators": meta.get("indicators", {})}
        prediction, _ = pnl_service.predict_for_symbol(
            symbol,
            client,
            settings,
            position_size_usdt=position_size,
            precomputed_signal=precomputed,
            trade_type=trade_type,
            trade_side=trade_side,
            current_price=current_price,
        )
        return prediction
    except Exception as exc:
        logger.warning(f"[StagedEntry] predictor run failed for {symbol}: {exc}")
        return None


def build_range_reversion_prediction(
    meta: Dict[str, Any],
    current_price: Optional[float],
    settings: Any,
) -> Dict[str, Any]:
    try:
        price = _number(current_price)
        if price is None:
            price = _number(meta.get("current_price") or meta.get("price"))
        if price is None or price <= 0:
            return {"valid": False, "reason": "missing_current_price"}
        source = dict(meta.get("indicators") or {})
        bollinger = source.get("bollinger_bands") or meta.get("bollinger_bands") or {}
        # analyze_signal stores BB as flat keys; support both shapes.
        if isinstance(bollinger, dict) and bollinger:
            lower = _number(bollinger.get("lower"))
            middle = _number(bollinger.get("middle"))
            upper = _number(bollinger.get("upper"))
        else:
            lower = _number(source.get("lower_band") or meta.get("lower_band"))
            middle = _number(source.get("middle_band") or meta.get("middle_band"))
            upper = _number(source.get("upper_band") or meta.get("upper_band"))
        if middle is None and lower is not None and upper is not None:
            middle = (lower + upper) / 2.0
        if lower is None or middle is None or upper is None:
            return {"valid": False, "reason": "missing_bollinger_bands"}
        source["bollinger_bands"] = {"lower": lower, "middle": middle, "upper": upper}
        for key, fallback_keys in {
            "rsi": ("rsi",),
            "vol_spike": ("vol_spike", "volume_ratio"),
            "trend_4h": ("trend_4h", "higher_tf_trend"),
        }.items():
            value = source.get(key)
            if value is None:
                for fallback_key in fallback_keys:
                    value = meta.get(fallback_key)
                    if value is not None:
                        break
            source[key] = value if isinstance(value, dict) else {"value": value}
        source["signal_strength"] = _number(
            source.get("signal_strength") or meta.get("signal_strength") or meta.get("strength")
        ) or 0.5
        trade_type = meta.get("trade_type") or "spot"
        trade_side = str(meta.get("expected_side") or ("SHORT" if trade_type == "futures" else "LONG")).strip().upper()
        import pnl_service
        return pnl_service.get_predictor().predict_range_reversion(
            current_price=price,
            indicators=source,
            position_size_usdt=_position_size_for_validation(meta),
            settings=settings,
            trade_type=trade_type,
            trade_side=trade_side,
        )
    except Exception as exc:
        logger.warning(f"[StagedEntry] range reversion prediction failed: {exc}")
        return {"valid": False, "reason": "range_reversion_prediction_error"}


def _range_reversion_shadow(meta: Dict[str, Any], current_price: Optional[float], settings: Any) -> Optional[Dict[str, Any]]:
    if str(getattr(settings, "range_reversion_predictor_mode", "shadow")).lower() == "disabled":
        return None
    return build_range_reversion_prediction(meta, current_price, settings)


def _evaluate_candidate_policy(
    meta: Dict[str, Any],
    regime_mode: str,
    cfg: Dict[str, Any],
    profile: Optional[str],
    quality_score: float,
    current_price: Optional[float],
    settings: Any,
) -> Tuple[bool, str, Dict[str, Any]]:
    shadow = _range_reversion_shadow(meta, current_price, settings) if regime_mode == "RANGE_SCALP" else None
    expected_pnl = meta.get("expected_pnl")
    probability = meta.get("success_probability")
    try:
        expected_pnl = float(expected_pnl) if expected_pnl is not None else None
    except (TypeError, ValueError):
        expected_pnl = None
    try:
        probability = float(probability) if probability is not None else None
    except (TypeError, ValueError):
        probability = None
    if profile not in ("A", "B", "C"):
        return False, "QUALITY_BELOW_C", {
            "policy_version": cfg.get("policy_version", "v1"),
            "regime": regime_mode,
            "profile": profile,
            "quality_score": quality_score,
            "expected_pnl": expected_pnl,
            "success_probability": probability,
            "range_reversion_shadow": shadow,
        }
    if shadow:
        meta["range_reversion_prediction"] = shadow
    decision = evaluate_entry_decision(
        meta=meta,
        regime=regime_mode,
        profile=profile,
        quality_score=quality_score,
        settings=settings,
        current_price=current_price,
    )
    policy = decision.to_dict()
    policy["range_reversion_shadow"] = shadow
    _position_size = _number(meta.get("position_size_usdt"))
    if _position_size is not None and _position_size > 0:
        policy["position_size_usdt"] = _position_size
    if not decision.eligible:
        return False, decision.reason, policy
    return True, decision.reason, policy


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value) if value is not None else None
        return number if number is not None and math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _direction_mode(cfg: Dict[str, Any]) -> str:
    return str(cfg.get("direction_confirmation_mode", "disabled")).strip().lower()


def _direction_transition(
    row: Dict[str, Any],
    policy: Dict[str, Any],
    current_price: Optional[float],
    cfg: Dict[str, Any],
) -> Tuple[bool, bool, str, Dict[str, Any]]:
    data = row.get("data") or {}
    direction = dict(data.get("direction") or {}) if isinstance(data, dict) else {}
    mode = _direction_mode(cfg)
    side = str(direction.get("side") or _side_for_regime(str(row.get("regime_mode") or ""))).upper()
    base_price = _number(direction.get("base_price")) or _number(row.get("signal_price"))
    price = _number(current_price)
    state = str(direction.get("state") or "awaiting_confirmation")
    stored_target = _number(direction.get("target_price"))
    stored_stop = _number(direction.get("stop_price"))
    raw_target = _number(policy.get("target_price"))
    raw_stop = _number(policy.get("stop_price"))
    if stored_target is not None and stored_stop is not None:
        target_price = stored_target
        stop_price = stored_stop
    else:
        target_price, stop_price = _align_target_stop_with_side(base_price, raw_target, raw_stop, side)
    last_price = _number(direction.get("last_observed_price")) or base_price
    min_progress_bps = max(0.0, float(cfg.get("direction_min_progress_bps", 0.0) or 0.0))
    max_adverse_bps = max(0.0, float(cfg.get("direction_max_adverse_bps", 0.0) or 0.0))
    # s111: anchor both thresholds to this candidate's own stop distance instead of using
    # a flat bps figure identical for every symbol/volatility regime. Measured on 48h of
    # production data (RANGE_SCALP, direction_min_progress_bps=1.0/direction_max_adverse_bps=150):
    # 1,772 DIRECTION_CONFIRMED vs. 1,044 demotions (DIRECTION_RECOVERY_PENDING+DIRECTION_DRAWDOWN_SOFT)
    # — a candidate lost its confirmation ~59% as often as it earned one, because 1bps is
    # smaller than normal bid/ask noise (doesn't actually filter false breakouts) while
    # 150bps is a routine excursion inside a RANGE_SCALP's own range (doesn't reflect real
    # invalidation risk). Anchoring to `stop_distance_bps` (the real risk this specific trade
    # is taking) makes both thresholds meaningful without another symbol-by-symbol constant.
    # Opt-in per regime (default False) — only RANGE_SCALP enables this today (see
    # config.py/config_helpers.py staged_entry_regime_config). Other regimes already
    # use fixed bps values proportionate to their own calibration (5/50, 3/50, 3/50)
    # and existing tests assert that fixed-bps behavior explicitly.
    if (
        bool(cfg.get("direction_geometry_anchor_enabled", False))
        and stop_price is not None and stop_price > 0
        and base_price is not None and base_price > 0
    ):
        stop_distance_bps = abs(base_price - stop_price) / base_price * 10_000.0
        if stop_distance_bps > 0:
            _min_frac = max(0.0, float(cfg.get("direction_min_progress_stop_fraction", 0.05) or 0.05))
            _max_frac = max(0.0, float(cfg.get("direction_max_adverse_stop_fraction", 0.6) or 0.6))
            min_progress_bps = max(min_progress_bps, stop_distance_bps * _min_frac)
            if max_adverse_bps > 0:
                max_adverse_bps = min(max_adverse_bps, stop_distance_bps * _max_frac)
    required = max(1, int(cfg.get("direction_required_confirmations", 2) or 2))
    # s113: volume corroboration (VSA-inspired). Retrospective replay of 48h of real
    # closes found the worst false-breakout losses (GIGGLEUSDT) were preceded by an
    # anomalous volume spike (~10x baseline) shortly before the signal was created —
    # a classic "shakeout" signature, not a clean level test. `vol_spike` is read
    # once from the signal-time metadata (no extra per-tick API calls — avoids
    # reintroducing the s106-style cadence bottleneck from adding a live volume
    # fetch to the 10s watcher loop). Shadow by default (`direction_volume_gate_enabled`
    # defaults to False everywhere): always recorded in the snapshot for measurement;
    # only added to `required` (never removes confirmations already needed) when a
    # regime explicitly opts in after validating against real outcomes.
    _signal_meta_for_vol = row.get("signal_meta") if isinstance(row.get("signal_meta"), dict) else {}
    vol_spike_at_signal = _number(
        (data.get("vol_spike") if isinstance(data, dict) else None)
        if data.get("vol_spike") is not None
        else _signal_meta_for_vol.get("vol_spike")
    )
    _vol_anomaly_mult = max(1.0, float(cfg.get("direction_volume_anomaly_multiplier", 3.0) or 3.0))
    volume_anomaly = bool(vol_spike_at_signal is not None and vol_spike_at_signal >= _vol_anomaly_mult)
    if bool(cfg.get("direction_volume_gate_enabled", False)) and volume_anomaly:
        required += 1
    support_level = _number(data.get("support_level")) if isinstance(data, dict) else None
    resistance_level = _number(data.get("resistance_level")) if isinstance(data, dict) else None
    sr_invalidation_pct = max(0.0, float(cfg.get("direction_sr_invalidation_pct", 0.0) or 0.0)) / 100.0
    confirmations = int(direction.get("directional_confirmations", 0) or 0)
    adverse_observations = int(direction.get("adverse_observations", 0) or 0)
    snapshot: Dict[str, Any] = {
        "mode": mode,
        "state_before": state,
        "side": side,
        "base_price": base_price,
        "previous_price": last_price,
        "current_price": price,
        "target_price": target_price,
        "stop_price": stop_price,
        "vol_spike_at_signal": vol_spike_at_signal,
        "volume_anomaly": volume_anomaly,
        "directional_confirmations": confirmations,
        "adverse_observations": adverse_observations,
        "required_confirmations": required,
        "min_progress_bps": min_progress_bps,
        "max_adverse_bps": max_adverse_bps,
    }
    if mode == "disabled":
        snapshot["state_after"] = state
        return True, False, "DIRECTION_DISABLED", snapshot
    if price is None or price <= 0:
        snapshot["state_after"] = state
        snapshot["direction"] = direction
        return False, True, "DIRECTION_PRICE_MISSING", snapshot
    if base_price is None or base_price <= 0 or last_price is None or last_price <= 0:
        snapshot["state_after"] = state
        snapshot["direction"] = direction
        return False, True, "DIRECTION_REFERENCE_MISSING", snapshot
    is_short = side == "SHORT"
    direction_sign = -1.0 if is_short else 1.0
    progress_bps = direction_sign * (price - last_price) / last_price * 10_000.0
    base_progress_bps = direction_sign * (price - base_price) / base_price * 10_000.0
    snapshot["progress_bps"] = progress_bps
    snapshot["base_progress_bps"] = base_progress_bps
    updated = dict(direction)
    updated.update({
        "base_price": base_price,
        "side": side,
        "target_price": target_price,
        "stop_price": stop_price,
        "last_observed_price": price,
        "last_observed_at": _now().isoformat(),
    })
    if row.get("regime_mode") == "RANGE_SCALP":
        if target_price is None or target_price <= 0:
            snapshot["state_after"] = state
            snapshot["direction"] = updated
            return False, True, "DIRECTION_TARGET_MISSING", snapshot
        # Validate target orientation against the staged reference price, not the
        # current tick. If we compare against current price, valid breakouts that
        # already crossed the target get incorrectly rejected as invalid.
        if (not is_short and target_price <= base_price) or (is_short and target_price >= base_price):
            snapshot["state_after"] = state
            snapshot["direction"] = updated
            return False, True, "DIRECTION_TARGET_INVALID", snapshot
    # Explicit, clearly-labeled invalidation path: the price moved decisively past
    # the support/resistance level the setup was staged against (not just a soft
    # pullback). This is the ONLY structural condition — besides a stop breach —
    # allowed to discard a candidate before it reaches max_age_min/max_cycles, so
    # a candidate legitimately waiting for a breakout is never killed by a timer.
    if sr_invalidation_pct > 0:
        if is_short and resistance_level is not None and resistance_level > 0:
            invalidation_price = resistance_level * (1.0 + sr_invalidation_pct)
            if price >= invalidation_price:
                updated["sr_proximity_lost"] = {
                    "price": price,
                    "resistance_level": resistance_level,
                    "invalidation_price": invalidation_price,
                    "side": side,
                    "at": _now().isoformat(),
                }
                snapshot["state_after"] = state
                snapshot["direction"] = updated
                return False, True, "DIRECTION_SR_PROXIMITY_LOST", snapshot
        elif not is_short and support_level is not None and support_level > 0:
            invalidation_price = support_level * (1.0 - sr_invalidation_pct)
            if price <= invalidation_price:
                updated["sr_proximity_lost"] = {
                    "price": price,
                    "support_level": support_level,
                    "invalidation_price": invalidation_price,
                    "side": side,
                    "at": _now().isoformat(),
                }
                snapshot["state_after"] = state
                snapshot["direction"] = updated
                return False, True, "DIRECTION_SR_PROXIMITY_LOST", snapshot
    if stop_price is not None and ((not is_short and price <= stop_price) or (is_short and price >= stop_price)):
        # Clear opposite-side SR breach — record the event and discard.
        updated["opposite_side_breach"] = {
            "price": price,
            "stop_price": stop_price,
            "side": side,
            "at": _now().isoformat(),
        }
        snapshot["state_after"] = state
        snapshot["direction"] = updated
        return False, True, "DIRECTION_STOP_BREACHED", snapshot
    if max_adverse_bps and base_progress_bps <= -max_adverse_bps:
        # Always give a recovery chance; only a clear stop/SR breach discards a live setup.
        # s111: soft-decrement instead of a hard reset to 0 — a candidate that already
        # earned several confirmations keeps most of that progress on a single adverse
        # excursion (state still demotes to recovery_pending either way, so this doesn't
        # skip re-validating the move; it only avoids discarding confirmations already
        # earned when `direction_required_confirmations` > 1, e.g. SPOT_BULL/FUTURES_SHORT).
        updated["adverse_observations"] = adverse_observations + 1
        updated["directional_confirmations"] = max(0, confirmations - 1)
        updated["state"] = "recovery_pending"
        snapshot["state_after"] = updated["state"]
        snapshot["direction"] = updated
        return False, False, "DIRECTION_DRAWDOWN_SOFT", snapshot
    target_progress = True
    if target_price is not None:
        if is_short:
            target_progress = (price <= target_price) or (abs(target_price - price) < abs(target_price - last_price))
        else:
            target_progress = (price >= target_price) or (abs(target_price - price) < abs(target_price - last_price))
    snapshot["target_progress"] = target_progress
    # s108/Opcion F: on the very FIRST observation for this setup (no prior
    # tick recorded — `last_observed_price` still equals `base_price`), also
    # accept cumulative progress since the signal was detected
    # (`base_progress_bps`) as a valid confirmation trigger, not only a fresh
    # tick-to-tick move. Without this, a setup that had already moved well
    # past the confirmation threshold between signal detection and the first
    # watcher/inter-cycle check (e.g. price genuinely fell before anyone
    # looked at it again) discarded that entire head start and had to wait for
    # a brand-new favorable tick from wherever the first observed price
    # happened to be. This is intentionally scoped to the first observation
    # only: once ticks are being observed normally, confirmation keeps using
    # the tick-to-tick reference so the s106 noise-hold behavior (a single
    # tick under the noise threshold must not reset or fast-forward progress)
    # is unaffected.
    is_first_observation = abs(last_price - base_price) < 1e-12
    favorable = (
        progress_bps >= min_progress_bps
        or (is_first_observation and base_progress_bps >= min_progress_bps)
    ) and target_progress
    if favorable:
        if state == "recovery_pending":
            confirmations = 1
            updated["state"] = "confirmed" if confirmations >= required else "recovery_confirming"
        else:
            confirmations += 1
            updated["state"] = "confirmed" if confirmations >= required else "awaiting_confirmation"
        updated["adverse_observations"] = 0
        updated["directional_confirmations"] = confirmations
        updated["last_confirmed_price"] = price
        updated["last_confirmed_at"] = _now().isoformat()
        if confirmations >= required and not updated.get("first_confirmed_price"):
            # s108/Opcion E: stamped once, the first time this setup ever
            # reaches "confirmed" — unlike `last_confirmed_price` (overwritten
            # on every favorable tick), this survives all the way to execution
            # so the wait cost (first-confirmed price vs. actual fill price)
            # can be measured and persisted (see `_build_entry_context`).
            updated["first_confirmed_price"] = price
            updated["first_confirmed_at"] = _now().isoformat()
        snapshot["state_after"] = updated["state"]
        snapshot["directional_confirmations"] = confirmations
        snapshot["direction"] = updated
        if confirmations >= required:
            return True, False, "DIRECTION_CONFIRMED", snapshot
        return False, False, "DIRECTION_CONFIRMING", snapshot
    if abs(progress_bps) < min_progress_bps:
        # s106: pure noise — the tick moved less than the minimum progress
        # threshold in EITHER direction (not a real adverse move). Do NOT
        # consume an adverse_observation nor reset directional_confirmations:
        # with direction_min_progress_bps as tight as 1bps (RANGE_SCALP),
        # ordinary bid/ask noise was being treated identically to a genuine
        # reversal, resetting confirmations already earned and forcing the
        # setup to re-earn them from zero on almost every tick. Measured on
        # 24h of production data: 27.9% of all monitoring ticks fell in this
        # noise band, and candidates needed ~19 confirm/reset cycles on
        # average before an execution attempt finally caught a stable
        # "confirmed" window (see calibracion_entradas-a20ab8-v3.md s106).
        # Mirrors directional_confirmation.py's DIRECTION_V2_NOISE_HOLD
        # (already validated in shadow via directional_policy_mode).
        snapshot["state_after"] = state
        snapshot["direction"] = updated
        return False, False, "DIRECTION_NOISE_HOLD", snapshot
    max_adverse = max(1, int(cfg.get("direction_max_adverse_observations", 3) or 3))
    adverse_observations += 1
    updated["adverse_observations"] = adverse_observations
    # s111: soft-decrement, see the DIRECTION_DRAWDOWN_SOFT branch above for rationale.
    updated["directional_confirmations"] = max(0, confirmations - 1)
    if adverse_observations < max_adverse:
        updated["state"] = "recovery_pending"
        snapshot["state_after"] = updated["state"]
        snapshot["direction"] = updated
        return False, False, "DIRECTION_RECOVERY_PENDING", snapshot
    # If the trigger has not progressed yet, keep monitoring until max_age or
    # max_cycles expire. Do not terminal-discard only because breakout is pending.
    updated["state"] = "awaiting_confirmation"
    snapshot["state_after"] = updated["state"]
    snapshot["direction"] = updated
    return False, False, "DIRECTION_WAITING_BREAKOUT", snapshot


def _combine_directional_policy(
    row: Dict[str, Any],
    policy_eligible: bool,
    policy_reason: str,
    policy_data: Dict[str, Any],
    current_price: Optional[float],
    cfg: Dict[str, Any],
) -> Tuple[bool, bool, str, Dict[str, Any]]:
    if not policy_eligible:
        return False, True, policy_reason, {}
    direction_positive, direction_terminal, direction_reason, direction_snapshot = _direction_transition(
        row, policy_data, current_price, cfg
    )
    if _direction_mode(cfg) != "enforced":
        return True, False, policy_reason, direction_snapshot
    return direction_positive, direction_terminal, direction_reason, direction_snapshot


def _input_features_for_check(
    meta: Dict[str, Any],
    current_price: Optional[float],
    signal_price: Optional[float],
) -> Dict[str, Any]:
    """Return the subset of features that must be persisted for each check."""
    features = {
        "expected_pnl": meta.get("expected_pnl"),
        "expected_pnl_pct": meta.get("expected_pnl_percentage") or meta.get("expected_pnl_pct"),
        "success_probability": meta.get("success_probability"),
        "peak_quality": meta.get("peak_quality"),
        "composite_score": meta.get("composite_score"),
        "confluence": meta.get("confluence"),
        "short_term_score": meta.get("short_term_score"),
        "range_score": meta.get("range_score"),
        "vol_score": meta.get("vol_score"),
        "rsi_score": meta.get("rsi_score"),
        "symbol_quality_score": meta.get("symbol_quality_score"),
        "rsi_4h": meta.get("rsi_4h"),
        "vol_spike": meta.get("vol_spike"),
        "range_position": meta.get("range_position"),
        "trend_4h": meta.get("trend_4h"),
        "atr": meta.get("atr"),
        "signal_strength": meta.get("signal_strength") or meta.get("strength"),
        "ticker_price_change_pct": meta.get("ticker_price_change_pct"),
        "category": meta.get("category"),
        "sr_zone_position": meta.get("sr_zone_position"),
        "sr_support_dist_pct": meta.get("nearest_support_dist_pct") or meta.get("sr_support_dist_pct"),
        "sr_resistance_dist_pct": meta.get("nearest_resistance_dist_pct") or meta.get("sr_resistance_dist_pct"),
        "support_bounce_buy": meta.get("support_bounce_buy") or meta.get("sr_bounce"),
        "support_breakout_buy": meta.get("support_breakout_buy") or meta.get("sr_breakout"),
        "support_breakdown_sell": meta.get("support_breakdown_sell") or meta.get("sr_breakdown"),
        "support_strength": meta.get("support_strength"),
        "support_volume_ratio": meta.get("support_volume_ratio"),
        "current_price": current_price,
        "signal_price": signal_price,
    }
    _cp = float(current_price) if current_price is not None else None
    _sp = float(signal_price) if signal_price is not None else None
    if _cp is not None and _sp is not None and _sp > 0:
        features["price_delta_pct"] = (_cp - _sp) / _sp * 100.0
    return {k: v for k, v in features.items() if v is not None}


def load_ready_candidates(
    db_client: Any,
    regime_mode: Optional[str] = None,
    status: Optional[Tuple[str, ...]] = None,
    symbol: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return active ready/validated rows."""
    if status is None:
        status = ("ready", "validated")
    sql = "SELECT * FROM ready_candidates WHERE status IN %s"
    params: List[Any] = [status]
    if regime_mode:
        sql += " AND regime_mode = %s"
        params.append(regime_mode)
    if symbol:
        sql += " AND symbol = %s"
        params.append(symbol)
    sql += " ORDER BY ready_at ASC"
    try:
        rows = db_client.execute_query(sql, tuple(params))
        if not rows:
            return []
        for row in rows:
            for key in ("signal_meta", "config_snapshot", "data"):
                if row.get(key) and isinstance(row[key], str):
                    try:
                        row[key] = json.loads(row[key])
                    except Exception:
                        pass
        return rows
    except Exception as exc:
        logger.warning(f"[StagedEntry] load_ready_candidates failed: {exc}")
        return []


def _next_check_sequence(db_client: Any, ready_id: int) -> int:
    try:
        rows = db_client.execute_query(
            "SELECT COALESCE(MAX(check_sequence), 0) + 1 AS seq FROM ready_candidate_checks WHERE ready_candidate_id = %s",
            (ready_id,),
        )
        return int(rows[0]["seq"]) if rows else 1
    except Exception:
        return 1


def _record_check(
    db_client: Any,
    ready_id: int,
    cycle_id: Optional[str],
    check_sequence: int,
    check_type: str,
    current_price: Optional[float],
    price_source: Optional[str],
    profile: Optional[str],
    quality_score: float,
    thresholds: Optional[Dict[str, Any]],
    fear_greed: Optional[float],
    input_features: Dict[str, Any],
    positive: bool,
    decision: str,
    reason: str,
    execution_ms: int,
    writer: Optional[str] = None,
) -> None:
    if not db_client:
        return
    try:
        db_client.execute_update(
            """
            INSERT INTO ready_candidate_checks (
                ready_candidate_id, cycle_id, check_sequence, check_type, checked_at,
                current_price, price_source, expected_pnl_pct, quality_score, profile_label,
                thresholds_snapshot, fear_greed, input_features, positive, decision, reason, execution_ms, writer
            ) VALUES (
                %s, %s, %s, %s, NOW(),
                %s, %s, %s, %s, %s,
                %s::jsonb, %s, %s::jsonb, %s, %s, %s, %s, %s
            )
            """,
            (
                int(ready_id) if ready_id is not None else None,
                cycle_id,
                int(check_sequence) if check_sequence is not None else None,
                check_type,
                float(current_price) if current_price is not None else None,
                price_source,
                float(input_features.get("expected_pnl_pct")) if input_features.get("expected_pnl_pct") is not None else None,
                float(quality_score) if quality_score is not None else None,
                profile,
                _json(thresholds) if thresholds else "{}",
                float(fear_greed) if fear_greed is not None else None,
                _json(input_features),
                bool(positive),
                decision,
                reason,
                int(execution_ms) if execution_ms is not None else None,
                writer,
            ),
        )
    except Exception as exc:
        logger.warning(f"[StagedEntry] _record_check failed for ready_id={ready_id}: {exc}")


def _update_ready_status(
    db_client: Any,
    ready_id: int,
    status: Optional[str] = None,
    positive_checks: Optional[int] = None,
    total_checks: Optional[int] = None,
    last_checked_at: bool = True,
    validated_at: bool = False,
    opened_at: bool = False,
    opened_trade_id: Optional[int] = None,
    discard_reason: Optional[str] = None,
    discarded_at: bool = False,
    data_update: Optional[Dict[str, Any]] = None,
    guard_status: Optional[str] = None,
    expected_revision: Optional[int] = None,
) -> bool:
    """Update a ready_candidates row.

    Returns True if the write was applied, False if it was skipped (no DB
    client, nothing to update, or an optimistic-concurrency conflict was
    detected via `guard_status`/`expected_revision`). Callers that made a
    decision based on a previously-read row should pass `expected_revision`
    (the `candidate_revision` observed at read time) so a concurrent writer
    (e.g. the intra-cycle watcher vs. the main cycle) cannot silently clobber
    a newer state with a stale decision.
    """
    if not db_client:
        return False
    current_rev: Optional[int] = None
    if data_update or expected_revision is not None:
        try:
            rev_rows = db_client.execute_query(
                "SELECT COALESCE((data->>'candidate_revision')::int, 0) AS rev FROM ready_candidates WHERE id = %s",
                (int(ready_id),),
            ) or []
            current_rev = int((rev_rows[0].get("rev") if rev_rows else 0) or 0)
        except Exception:
            current_rev = None
    if expected_revision is not None and current_rev is not None and current_rev != int(expected_revision):
        logger.warning(
            f"[StagedEntry] CONCURRENT_REVISION_CONFLICT ready_id={ready_id} "
            f"expected_revision={expected_revision} actual_revision={current_rev} - write skipped"
        )
        return False
    fields: List[str] = []
    params: List[Any] = []
    if status:
        fields.append("status = %s")
        params.append(status)
    if positive_checks is not None:
        fields.append("positive_checks = %s")
        params.append(int(positive_checks))
    if total_checks is not None:
        fields.append("total_checks = %s")
        params.append(int(total_checks))
    if last_checked_at:
        fields.append("last_checked_at = NOW()")
    if validated_at:
        fields.append("validated_at = NOW()")
    if opened_at:
        fields.append("opened_at = NOW()")
    if opened_trade_id is not None:
        fields.append("opened_trade_id = %s")
        params.append(int(opened_trade_id))
    if discard_reason:
        fields.append("discard_reason = %s")
        params.append(discard_reason)
    if discarded_at:
        fields.append("discarded_at = NOW()")
    if data_update:
        data_update = dict(data_update)
        data_update["candidate_revision"] = (current_rev or 0) + 1
        data_update["candidate_revision_updated_at"] = _now().isoformat()
        fields.append("data = data || %s::jsonb")
        params.append(_json(data_update))
    if not fields:
        return False
    sql = "UPDATE ready_candidates SET " + ", ".join(fields) + ", updated_at = NOW() WHERE id = %s"
    params.append(int(ready_id))
    if guard_status:
        sql += " AND status = %s"
        params.append(guard_status)
    if expected_revision is not None:
        sql += " AND (data->>'candidate_revision')::int = %s"
        params.append(int(expected_revision))
    try:
        affected = db_client.execute_update(sql, tuple(params))
        if expected_revision is not None and not affected:
            logger.warning(
                f"[StagedEntry] CONCURRENT_REVISION_CONFLICT ready_id={ready_id} "
                f"(write raced at commit time, 0 rows affected)"
            )
            return False
        return True
    except Exception as exc:
        logger.warning(f"[StagedEntry] _update_ready_status failed for ready_id={ready_id}: {exc}")
        return False


def record_execution_gate(
    db_client: Any,
    ready_id: Optional[int],
    passed: bool,
    reason: str,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    if not db_client or ready_id is None:
        return
    payload = {
        "last_execution_gate": {
            "passed": bool(passed),
            "reason": str(reason),
            "details": _to_native(details or {}),
            "checked_at": _now().isoformat(),
        }
    }
    _update_ready_status(db_client, int(ready_id), data_update=payload)


def _fetch_one(db_client: Any, sql: str, params: Tuple[Any, ...]) -> Optional[Dict[str, Any]]:
    try:
        rows = db_client.execute_query(sql, params)
        return rows[0] if rows else None
    except Exception:
        return None


def _build_initial_direction(
    signal_price: Optional[float],
    meta: Dict[str, Any],
    policy_data: Dict[str, Any],
    regime_mode: str,
) -> Dict[str, Any]:
    """Build the initial direction dict for a staged candidate.

    When a directional arbitration decision exists (FUTURES_SHORT, FUTURES_LONG,
    SPOT_LONG), derive target_price and stop_price from the selected prediction
    so the direction FSM receives coherent side + target/stop pairs.
    """
    trade_type = _trade_type_for_meta(meta, regime_mode)
    side = _side_for_meta(meta, regime_mode, trade_type)
    target_price = policy_data.get("target_price")
    stop_price = policy_data.get("stop_price")

    arb = meta.get("directional_arbitration") or {}
    selected = arb.get("selected_mode")
    if selected:
        pred_key = {
            "FUTURES_SHORT": "short_prediction",
            "FUTURES_LONG": "futures_long_prediction",
            "SPOT_LONG": "long_prediction",
        }.get(selected)
        pred = arb.get(pred_key) or {} if pred_key else {}
        _arb_target = _number(pred.get("target_price"))
        _arb_stop = _number(pred.get("stop_price"))
        if _arb_target is None or _arb_stop is None:
            pr = pred.get("price_range") or {}
            _pr_min = _number(pr.get("min"))
            _pr_max = _number(pr.get("max"))
            if _pr_min is not None and _pr_max is not None:
                if side == "SHORT":
                    _arb_target = _arb_target or _pr_min
                    _arb_stop = _arb_stop or _pr_max
                else:
                    _arb_target = _arb_target or _pr_max
                    _arb_stop = _arb_stop or _pr_min
        if _arb_target is not None:
            target_price = _arb_target
        if _arb_stop is not None:
            stop_price = _arb_stop

    target_price, stop_price = _align_target_stop_with_side(
        signal_price,
        target_price,
        stop_price,
        side,
    )

    return {
        "state": "awaiting_confirmation",
        "base_price": float(signal_price) if signal_price is not None else None,
        "last_observed_price": float(signal_price) if signal_price is not None else None,
        "side": side,
        "target_price": target_price,
        "stop_price": stop_price,
        "directional_confirmations": 0,
        "adverse_observations": 0,
    }


def stage_ready_candidates(
    top_signals: List[Any],
    predictions: Dict[str, Any],
    signal_meta: Dict[str, Any],
    regime_mode: str,
    db_client: Any,
    cycle_id: Optional[str] = None,
    settings: Any = None,
    news_client: Optional[Any] = None,
    client: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Stage new ready candidates from the QUBO-selected top signals.

    Each staged candidate receives an initial `inter` check that counts as the
    first positive check (the selection itself was already gate-positive).
    Existing ready/validated rows are left untouched because the inter-cycle
    re-evaluation handles them.
    """
    if not db_client:
        return []

    cfg = get_staged_entry_regime_config(settings, regime_mode)
    if not cfg.get("enabled"):
        return []

    trade_type = _trade_type_for_regime(regime_mode)
    fear_greed = _get_fear_greed_value(news_client)
    staged: List[Dict[str, Any]] = []

    for cand in top_signals:
        symbol = _symbol_from_candidate(cand)
        if not symbol:
            continue

        meta = _build_meta_for_symbol(symbol, signal_meta, predictions, cand)
        trade_type = _trade_type_for_meta(meta, regime_mode)
        meta["trade_type"] = trade_type
        meta["expected_side"] = _side_for_meta(meta, regime_mode, trade_type)
        if str(regime_mode or "").upper() == "RANGE_SCALP":
            try:
                from range_scalp_data_contract import attach_snapshot
                attach_snapshot(meta)
            except Exception as exc:
                logger.warning(f"[StagedEntry] RANGE_SCALP feature contract failed for {symbol}: {exc}")

        # Ensure we capture the gate result; if the pipeline did not set it,
        # classify locally using the same metadata.
        if meta.get("peak_quality_profile") not in ("A", "B", "C"):
            _profile, _score, _info = _classify_with_gate(meta, regime_mode, symbol, settings, fear_greed)
            if _profile in ("A", "B", "C"):
                meta["peak_quality_profile"] = _profile
                meta["peak_quality_score"] = _score
                meta["entry_profile_gate_info"] = _info

        existing = _fetch_one(
            db_client,
            "SELECT id FROM ready_candidates WHERE symbol=%s AND trade_type=%s AND regime_mode=%s AND status IN ('ready','validated')",
            (symbol, trade_type, regime_mode),
        )
        if existing:
            # Inter-cycle evaluation owns existing rows.
            continue

        signal_price = meta.get("current_price") or meta.get("price")
        if signal_price is None and client is not None:
            signal_price = _get_price(client, symbol, cfg.get("price_source", "avg"))
        cand_cfg = _adapt_cfg_for_sr_signal(cfg, meta)
        position_size = _position_size_for_validation(meta)
        eligible, policy_reason, policy_data = _evaluate_candidate_policy(
            meta, regime_mode, cand_cfg, meta.get("peak_quality_profile"),
            float(meta.get("peak_quality_score", 0.0) or 0.0), signal_price, settings,
        )
        if not eligible:
            continue
        position_size = _position_size_for_validation(meta)
        required = int(cand_cfg.get("required_positive_checks", 2))
        checks_per_cycle = int(cand_cfg.get("checks_per_cycle", 1))
        max_cycles = int(cand_cfg.get("max_cycles", 2))
        max_age = int(cand_cfg.get("max_age_min", 25))
        initial_stage = _stage_payload(
            {},
            stage_kind="setup_detected",
            stage_reason="SETUP_CAPTURED",
            max_age_min=max_age,
        )

        _initial_direction_mode = _direction_mode(cand_cfg)
        _initial_positive = _initial_direction_mode != "enforced"
        try:
            rows = db_client.execute_query(
                """
                INSERT INTO ready_candidates (
                    symbol, trade_type, regime_mode, profile_label, signal_price, position_size_usdt,
                    status, positive_checks, total_checks, required_positive_checks, checks_per_cycle,
                    max_cycles, max_age_min, ready_at, last_checked_at, signal_meta, config_snapshot, data
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    'ready', %s, 1, %s, %s,
                    %s, %s, NOW(), NOW(), %s::jsonb, %s::jsonb, %s::jsonb
                ) RETURNING id
                """,
                (
                    symbol, trade_type, regime_mode,
                    meta.get("peak_quality_profile"),
                    float(signal_price) if signal_price is not None else None,
                    float(position_size) if position_size is not None else None,
                    int(_initial_positive),
                    int(required) if required is not None else None,
                    int(checks_per_cycle) if checks_per_cycle is not None else None,
                    int(max_cycles) if max_cycles is not None else None,
                    int(max_age) if max_age is not None else None,
                    _json(meta), _json(cand_cfg), _json({
                        "cycles_active": 1,
                        "candidate_revision": 1,
                        "candidate_revision_updated_at": _now().isoformat(),
                        **initial_stage,
                        "policy": policy_data,
                        "priority": policy_data.get("priority"),
                        "direction": _build_initial_direction(
                            signal_price, meta, policy_data, regime_mode,
                        ),
                        "sr_zone_position": meta.get("sr_zone_position"),
                        "nearest_support_dist_pct": meta.get("nearest_support_dist_pct"),
                        "nearest_resistance_dist_pct": meta.get("nearest_resistance_dist_pct"),
                        "sr_support_dist_pct": meta.get("sr_support_dist_pct"),
                        "sr_resistance_dist_pct": meta.get("sr_resistance_dist_pct"),
                        "support_level": meta.get("support_level"),
                        "resistance_level": meta.get("resistance_level"),
                        "support_bounce_buy": meta.get("support_bounce_buy") or meta.get("sr_bounce"),
                        "support_breakout_buy": meta.get("support_breakout_buy") or meta.get("sr_breakout"),
                        "support_breakdown_sell": meta.get("support_breakdown_sell") or meta.get("sr_breakdown"),
                    }),
                ),
            )
            ready_id = rows[0]["id"]
        except Exception as exc:
            logger.warning(f"[StagedEntry] failed to insert ready candidate {symbol}: {exc}")
            continue

        _record_check(
            db_client, ready_id, cycle_id, 0, "inter", signal_price, cand_cfg.get("price_source"),
            meta.get("peak_quality_profile"), float(meta.get("peak_quality_score", 0.0) or 0.0),
            (meta.get("entry_profile_gate_info") or {}).get("thresholds"),
            fear_greed, {**_input_features_for_check(meta, signal_price, signal_price), "policy": policy_data,
                         "direction": {"state_before": "setup", "state_after": "awaiting_confirmation"},
                         "stage_kind": "setup_detected", "stage_reason": "SETUP_CAPTURED"},
            _initial_positive, "setup_detected", policy_reason, 0,
        )
        staged.append({"id": ready_id, "symbol": symbol, "status": "ready", "new": True})

    return staged


def evaluate_inter_cycle(
    db_client: Any,
    client: Any,
    settings: Any,
    regime_mode: str,
    top_signals: List[Any],
    predictions: Dict[str, Any],
    signal_meta: Dict[str, Any],
    cycle_id: Optional[str] = None,
    news_client: Optional[Any] = None,
) -> Dict[str, List[str]]:
    """Run the inter-cycle validation for all active ready/validated candidates.

    Returns a summary with `opened`, `validated`, `discarded`, `kept` lists.
    """
    result = {"validated": [], "discarded": [], "kept": [], "opened": []}
    if not db_client:
        return result

    cfg = get_staged_entry_regime_config(settings, regime_mode)
    if not cfg.get("enabled"):
        return result

    fear_greed = _get_fear_greed_value(news_client)
    top_by_symbol = {_symbol_from_candidate(c): c for c in top_signals if _symbol_from_candidate(c)}

    readies = load_ready_candidates(db_client, regime_mode=regime_mode)
    if not readies:
        return result

    for row in readies:
        ready_id = row["id"]
        symbol = row["symbol"]
        start = time.monotonic()
        expected_revision = int((row.get("data") or {}).get("candidate_revision", 0) or 0)

        meta = _build_meta_for_symbol(symbol, signal_meta, predictions, top_by_symbol.get(symbol))
        meta.update(row.get("signal_meta") or {})
        trade_type = str(row.get("trade_type") or _trade_type_for_meta(meta, regime_mode)).strip().lower()
        meta["trade_type"] = trade_type
        meta["expected_side"] = _side_for_meta(meta, regime_mode, trade_type)

        # Advance cycle counter for every inter-cycle evaluation.
        _data = row.get("data") or {}
        _cycles_active = int(_data.get("cycles_active", 1)) + 1
        if isinstance(_data, dict):
            _data["cycles_active"] = _cycles_active

        # If the symbol is in the new top signals, its indicators/predictions
        # come from the already-computed pipeline for this cycle. But the price
        # used to *decide* (direction confirmation, target/stop distance, EV
        # re-check) must always be a fresh live quote — `meta.get("current_price")`
        # can be several minutes stale if this symbol's own indicator analysis
        # hasn't re-run since a previous cycle (s108: this caused a validated
        # candidate to accept/measure progress against a 12-minute-old price).
        in_top = symbol in top_by_symbol
        current_price = _get_price(client, symbol, cfg.get("price_source", "avg"))
        if current_price is None:
            # Live quote failed — fall back to whatever price the pipeline has.
            current_price = meta.get("current_price") or meta.get("price")
        if not in_top:
            # Re-run predictor for candidates that the pipeline did not select this cycle.
            pred = _run_predictor_for_validation(client, symbol, settings, meta, current_price)
            if pred:
                for key in ("expected_pnl", "expected_pnl_percentage", "position_size_usdt", "success_probability"):
                    if pred.get(key) is not None:
                        meta[key] = pred.get(key)

        profile, quality_score, gate_info = _classify_with_gate(
            meta, regime_mode, symbol, settings, fear_greed
        )
        signal_price = row.get("signal_price")
        input_features = _input_features_for_check(meta, current_price, signal_price)
        execution_ms = int((time.monotonic() - start) * 1000)

        cycle_cfg = _adapt_cfg_for_sr_signal(cfg, meta)
        policy_eligible, policy_reason, policy_data = _evaluate_candidate_policy(
            meta, regime_mode, cycle_cfg, profile, quality_score, current_price, settings,
        )
        row_status = str(row.get("status") or "ready").lower()
        if row_status == "validated":
            positive, terminal, reason, direction_snapshot = _combine_directional_policy(
                row, policy_eligible, policy_reason, policy_data, current_price, cycle_cfg,
            )
        else:
            positive = policy_eligible
            terminal = not policy_eligible
            reason = policy_reason
            direction_snapshot = {}
        input_features["policy"] = policy_data
        input_features["direction"] = direction_snapshot
        directional_shadow = evaluate_directional_shadow(row, policy_data, current_price, cycle_cfg)
        directional_shadow["legacy"] = {
            "positive": bool(positive),
            "terminal": bool(terminal),
            "reason": reason,
        }
        directional_shadow["would_change_execution"] = bool(
            (directional_shadow.get("action") == "CONFIRM") != bool(positive)
            or bool(directional_shadow.get("terminal")) != bool(terminal)
        )
        input_features["directional_shadow"] = directional_shadow
        total = row["total_checks"] + 1
        stage_update = _stage_payload(
            _data,
            stage_kind="trigger_monitoring",
            stage_reason="TRIGGER_RECHECK",
            max_age_min=int(row.get("max_age_min") or cycle_cfg.get("max_age_min") or 0),
        )
        data_update = {
            "cycles_active": _cycles_active,
            "policy": policy_data,
            "priority": policy_data.get("priority"),
            "sr_zone_position": meta.get("sr_zone_position"),
            "nearest_support_dist_pct": meta.get("nearest_support_dist_pct"),
            "nearest_resistance_dist_pct": meta.get("nearest_resistance_dist_pct"),
            "sr_support_dist_pct": meta.get("sr_support_dist_pct"),
            "sr_resistance_dist_pct": meta.get("sr_resistance_dist_pct"),
            "support_level": meta.get("support_level"),
            "resistance_level": meta.get("resistance_level"),
            "support_bounce_buy": meta.get("support_bounce_buy") or meta.get("sr_bounce"),
            "support_breakout_buy": meta.get("support_breakout_buy") or meta.get("sr_breakout"),
            "support_breakdown_sell": meta.get("support_breakdown_sell") or meta.get("sr_breakdown"),
            "directional_shadow_last": directional_shadow,
            **stage_update,
        }
        if direction_snapshot.get("direction"):
            data_update["direction"] = direction_snapshot["direction"]
        if positive:
            positives = row["positive_checks"] + 1
            if row_status == "validated":
                decision = "trigger_confirmed"
                applied = _update_ready_status(
                    db_client, ready_id, positive_checks=positives, total_checks=total, data_update=data_update,
                    expected_revision=expected_revision,
                )
                if applied:
                    expected_revision += 1
                    result["validated"].append(symbol)
                else:
                    decision = "concurrent_conflict"
                    result["kept"].append(symbol)
            else:
                decision = "trigger_confirmed"
                applied = _update_ready_status(
                    db_client, ready_id, positive_checks=positives, total_checks=total, data_update=data_update,
                    expected_revision=expected_revision,
                )
                if applied:
                    expected_revision += 1
                if not applied:
                    decision = "concurrent_conflict"
                    result["kept"].append(symbol)
                elif positives >= int(row["required_positive_checks"]):
                    validated_stage = _stage_payload(
                        data_update,
                        stage_kind="risk_authorized",
                        stage_reason="TRIGGER_FIRED_VALIDATED",
                        max_age_min=int(row.get("max_age_min") or cycle_cfg.get("max_age_min") or 0),
                    )
                    _update_ready_status(db_client, ready_id, status="validated", validated_at=True)
                    _update_ready_status(db_client, ready_id, data_update=validated_stage)
                    row["status"] = "validated"
                    result["validated"].append(symbol)
                else:
                    result["kept"].append(symbol)
        elif terminal:
            decision = "terminal_reject"
            data_update["last_rejection"] = {"reason": reason, "direction": direction_snapshot}
            data_update.update(
                _stage_payload(
                    data_update,
                    stage_kind="discarded",
                    stage_reason=_terminal_stage_reason(reason),
                    max_age_min=int(row.get("max_age_min") or cycle_cfg.get("max_age_min") or 0),
                )
            )
            applied = _update_ready_status(
                db_client, ready_id, total_checks=total, data_update=data_update,
                expected_revision=expected_revision,
            )
            if applied:
                expected_revision += 1
                _discard_ready(db_client, ready_id, reason, expected_revision=expected_revision)
                row["status"] = "discarded"
                result["discarded"].append(symbol)
            else:
                decision = "concurrent_conflict"
                result["kept"].append(symbol)
        else:
            decision = "trigger_pending"
            applied = _update_ready_status(
                db_client, ready_id, total_checks=total, data_update=data_update,
                expected_revision=expected_revision,
            )
            if applied:
                expected_revision += 1
            else:
                decision = "concurrent_conflict"
            result["kept"].append(symbol)

        input_features["stage_kind"] = data_update.get("stage_kind")
        input_features["stage_reason"] = data_update.get("stage_reason")
        _record_check(
            db_client, ready_id, cycle_id, _next_check_sequence(db_client, ready_id), "inter",
            current_price, cfg.get("price_source"), profile, quality_score,
            gate_info.get("thresholds"), fear_greed, input_features, positive, decision, reason,
            execution_ms, writer="main_cycle",
        )

        if _maybe_expire(db_client, row, expected_revision=expected_revision):
            if symbol in result["kept"]:
                result["kept"].remove(symbol)
            if symbol in result["validated"]:
                result["validated"].remove(symbol)
            if symbol not in result["discarded"]:
                result["discarded"].append(symbol)

    return result


def _recent_checks_of_type(db_client: Any, ready_id: int, check_type: str) -> List[Dict[str, Any]]:
    try:
        return db_client.execute_query(
            "SELECT check_type, positive FROM ready_candidate_checks WHERE ready_candidate_id=%s AND check_type=%s ORDER BY id DESC LIMIT 5",
            (ready_id, check_type),
        ) or []
    except Exception:
        return []


def _maybe_expire(
    db_client: Any,
    row: Dict[str, Any],
    expected_revision: Optional[int] = None,
) -> bool:
    """Discard a ready candidate if it exceeds max_age or max_cycles.

    Validated candidates are kept so they can still be opened this cycle.
    Returns True if the row was discarded.

    `expected_revision`, when provided, overrides the revision derived from
    `row["data"]["candidate_revision"]`. Callers that already performed a
    successful guarded write earlier in the same row-processing iteration
    must pass the up-to-date revision here (their in-memory `row` snapshot
    is not resynced after their own writes), otherwise this call would
    incorrectly detect a "conflict" against its own prior write.
    """
    ready_id = row["id"]
    status = row["status"]
    if status != "ready":
        return False
    if expected_revision is None:
        expected_revision = int((row.get("data") or {}).get("candidate_revision", 0) or 0)
    max_age = int(row.get("max_age_min") or 0)
    if max_age and row.get("ready_at"):
        age_min = (_now() - row["ready_at"]).total_seconds() / 60.0
        if age_min >= max_age:
            applied = _discard_ready(db_client, ready_id, "MAX_AGE_EXPIRED", expected_revision=expected_revision)
            if applied:
                row["status"] = "discarded"
                row["discard_reason"] = "MAX_AGE_EXPIRED"
                _record_check(
                    db_client, ready_id, None, _next_check_sequence(db_client, ready_id), "lifecycle",
                    None, None, None, 0.0, {}, None, {"age_min": age_min, "max_age_min": max_age},
                    False, "terminal_reject", "MAX_AGE_EXPIRED", 0, writer="expiry",
                )
            return applied
    data = row.get("data") or {}
    cycles_active = int(data.get("cycles_active", 1))
    max_cycles = int(row.get("max_cycles") or 0)
    if max_cycles and cycles_active >= max_cycles:
        applied = _discard_ready(db_client, ready_id, "MAX_CYCLES_REACHED", expected_revision=expected_revision)
        if applied:
            row["status"] = "discarded"
            row["discard_reason"] = "MAX_CYCLES_REACHED"
            _record_check(
                db_client, ready_id, None, _next_check_sequence(db_client, ready_id), "lifecycle",
                None, None, None, 0.0, {}, None, {"cycles_active": cycles_active, "max_cycles": max_cycles},
                False, "terminal_reject", "MAX_CYCLES_REACHED", 0, writer="expiry",
            )
        return applied
    return False


def _watcher_policy(direction: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    """Return a policy dict with coherent target/stop for the watcher.

    The watcher reuses the stored policy if it has target/stop; otherwise it
    falls back to the stored direction which was already aligned by side.
    """
    policy = dict(row.get("data") or {})
    policy = dict(policy.get("policy") or {})
    direction = dict((row.get("data") or {}).get("direction") or {})
    if policy.get("target_price") is None and policy.get("stop_price") is None:
        if direction.get("target_price") is not None and direction.get("stop_price") is not None:
            policy["target_price"] = direction["target_price"]
            policy["stop_price"] = direction["stop_price"]
    return policy


def _watcher_process_row(
    db_client: Any,
    row: Dict[str, Any],
    current_price: float,
    cfg: Dict[str, Any],
    validated: List[str],
) -> None:
    """Run a single watcher tick for one staged candidate.

    For validated candidates: updates the directional FSM against the current
    price, and a positive/terminal result gates execution/discard.

    For ready candidates (s108, Opcion H): the directional FSM ALSO runs (using
    the target/stop already computed at setup time, `_watcher_policy`), so a
    setup that breaks decisively in the adverse direction can be discarded
    immediately instead of waiting until it clears the quality/EV gate and
    reaches `validated` — and a setup that already confirms favorably during
    `ready` arrives at `validated` with `direction.state` already "confirmed",
    so the fast-path does not need to re-earn the confirmation from scratch.
    Quality/EV eligibility for the `ready` -> `validated` promotion itself is
    untouched: it is still decided exclusively by `evaluate_inter_cycle`.
    """
    ready_id = row["id"]
    symbol = row["symbol"]
    current_data = row.get("data") or {}
    expected_revision = int(current_data.get("candidate_revision", 0) or 0)
    policy_data = _watcher_policy(current_data.get("direction") or {}, row)
    row_status = str(row.get("status") or "ready").lower()

    start = time.monotonic()
    if row_status == "validated":
        positive, terminal, reason, direction_snapshot = _combine_directional_policy(
            row, True, "WATCHER_POLICY_REUSED", policy_data, current_price, cfg,
        )
    elif policy_data.get("target_price") is not None and policy_data.get("stop_price") is not None:
        positive, terminal, reason, direction_snapshot = _direction_transition(
            row, policy_data, current_price, cfg,
        )
    else:
        # No target/stop computed yet for this candidate (very first ticks
        # right after `setup_detected`) — nothing to measure progress against.
        positive = False
        terminal = False
        reason = "READY_PRICE_OBSERVED"
        direction_snapshot = {}
    execution_ms = int((time.monotonic() - start) * 1000)

    data_update: Dict[str, Any] = {
        "policy": policy_data,
        "directional_shadow_last": evaluate_directional_shadow(row, policy_data, current_price, cfg),
    }
    if direction_snapshot and direction_snapshot.get("direction"):
        data_update["direction"] = direction_snapshot["direction"]

    total = row["total_checks"] + 1

    if row_status == "validated":
        if positive:
            applied = _update_ready_status(
                db_client, ready_id, total_checks=total, data_update=data_update,
                expected_revision=expected_revision,
            )
            if applied:
                expected_revision += 1
                validated.append(symbol)
                decision = "trigger_confirmed"
            else:
                decision = "concurrent_conflict"
        elif terminal:
            data_update["last_rejection"] = {"reason": reason, "direction": direction_snapshot}
            applied = _update_ready_status(
                db_client, ready_id, total_checks=total, data_update=data_update,
                expected_revision=expected_revision,
            )
            if applied:
                expected_revision += 1
                _discard_ready(db_client, ready_id, reason, expected_revision=expected_revision)
                decision = "terminal_reject"
            else:
                decision = "concurrent_conflict"
        else:
            applied = _update_ready_status(
                db_client, ready_id, total_checks=total, data_update=data_update,
                expected_revision=expected_revision,
            )
            if applied:
                expected_revision += 1
            decision = "trigger_pending" if applied else "concurrent_conflict"
    else:
        if "direction" not in data_update:
            # No target/stop available yet — fall back to a plain price observation.
            direction_stored = current_data.get("direction") or {}
            updated_dir = dict(direction_stored)
            updated_dir["last_observed_price"] = current_price
            updated_dir["last_observed_at"] = _now().isoformat()
            data_update["direction"] = updated_dir
        if terminal:
            # s108/Opcion H: a `ready` candidate that already broke decisively
            # against the setup does not need to wait for the quality/EV gate
            # to discard it — the adverse structural signal is enough on its own.
            data_update["last_rejection"] = {"reason": reason, "direction": direction_snapshot}
            applied = _update_ready_status(
                db_client, ready_id, total_checks=total, data_update=data_update,
                expected_revision=expected_revision,
            )
            if applied:
                expected_revision += 1
                _discard_ready(db_client, ready_id, reason, expected_revision=expected_revision)
                decision = "terminal_reject"
            else:
                decision = "concurrent_conflict"
        else:
            # `positive` (DIRECTION_CONFIRMED) is intentionally NOT used here to
            # promote/open early: the ready->validated promotion stays governed
            # exclusively by `evaluate_inter_cycle`'s quality/EV gate. Persisting
            # the confirmed `direction` state means that once quality clears,
            # the row reaches `validated` already confirmed, so the fast-path
            # does not need to re-earn a fresh confirmation from scratch.
            applied = _update_ready_status(
                db_client, ready_id, total_checks=total, data_update=data_update,
                expected_revision=expected_revision,
            )
            if applied:
                expected_revision += 1
            decision = "trigger_confirmed" if (applied and positive) else ("trigger_pending" if applied else "concurrent_conflict")

    input_features = _input_features_for_check(current_data, current_price, row.get("signal_price"))
    input_features["policy"] = policy_data
    input_features["direction"] = direction_snapshot if direction_snapshot else data_update.get("direction", {})
    input_features["directional_shadow"] = data_update["directional_shadow_last"]
    if terminal:
        input_features["stage_kind"] = "discarded"
        input_features["stage_reason"] = _terminal_stage_reason(reason)
    elif row_status == "validated":
        input_features["stage_kind"] = "risk_authorized"
        input_features["stage_reason"] = "VALIDATED_RECHECK"
    else:
        input_features["stage_kind"] = "trigger_monitoring"
        input_features["stage_reason"] = "TRIGGER_RECHECK"

    _record_check(
        db_client, ready_id, None, _next_check_sequence(db_client, ready_id), "intra",
        current_price, cfg.get("price_source", "avg"), None, 0.0, {}, None, input_features,
        positive, decision, reason, execution_ms, writer="watcher",
    )

    _maybe_expire(db_client, row, expected_revision=expected_revision)


def _intra_watcher_check(
    db_client: Any,
    client: Any,
    settings: Any,
    regime_mode: str,
    news_client: Optional[Any] = None,
) -> List[str]:
    """Lightweight watcher: only price + direction, no predictor re-evaluation.

    Uses up to 10 workers to fetch current prices concurrently.
    """
    if not db_client or not client:
        return []
    cfg = get_staged_entry_regime_config(settings, regime_mode)
    if not cfg.get("enabled"):
        return []

    readies = load_ready_candidates(db_client, regime_mode=regime_mode, status=("ready", "validated"))
    if not readies:
        return []

    price_source = cfg.get("price_source", "avg")
    validated: List[str] = []

    # Fetch prices concurrently (network-bound, safe for up to 10 workers)
    symbol_to_price: Dict[str, Optional[float]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_symbol = {
            executor.submit(_get_price, client, row["symbol"], price_source): row["symbol"]
            for row in readies
        }
        for future in concurrent.futures.as_completed(future_to_symbol):
            symbol_to_price[future_to_symbol[future]] = future.result()

    for row in readies:
        current_price = symbol_to_price.get(row["symbol"])
        if current_price is None:
            continue
        _watcher_process_row(db_client, row, current_price, cfg, validated)

    return validated


def perform_intra_cycle_check(
    db_client: Any,
    client: Any,
    settings: Any,
    regime_mode: str,
    news_client: Optional[Any] = None,
) -> List[str]:
    """Run intra-cycle validation for active ready candidates.

    Returns a list of newly validated symbols.
    """
    validated: List[str] = []
    if not db_client:
        return validated

    cfg = get_staged_entry_regime_config(settings, regime_mode)
    if not cfg.get("enabled") or cfg.get("intra_mode") == "disabled":
        return validated

    if cfg.get("intra_mode") == "watcher":
        return _intra_watcher_check(db_client, client, settings, regime_mode, news_client=news_client)

    fear_greed = _get_fear_greed_value(news_client)
    readies = load_ready_candidates(db_client, regime_mode=regime_mode, status=("ready", "validated"))
    if not readies:
        return validated

    price_source = cfg.get("price_source", "avg")
    for row in readies:
        ready_id = row["id"]
        symbol = row["symbol"]
        start = time.monotonic()

        meta = dict(row.get("signal_meta") or {})
        trade_type = str(row.get("trade_type") or _trade_type_for_meta(meta, regime_mode)).strip().lower()
        meta["trade_type"] = trade_type
        meta["expected_side"] = _side_for_meta(meta, regime_mode, trade_type)

        current_price = _get_price(client, symbol, price_source)
        if current_price is None:
            continue

        if cfg.get("intra_mode") == "predictor":
            pred = _run_predictor_for_validation(client, symbol, settings, meta, current_price)
            if pred:
                for key in ("expected_pnl", "expected_pnl_percentage", "position_size_usdt", "success_probability"):
                    if pred.get(key) is not None:
                        meta[key] = pred.get(key)

        profile, quality_score, gate_info = _classify_with_gate(
            meta, regime_mode, symbol, settings, fear_greed
        )
        signal_price = row.get("signal_price")
        input_features = _input_features_for_check(meta, current_price, signal_price)
        execution_ms = int((time.monotonic() - start) * 1000)

        policy_eligible, policy_reason, policy_data = _evaluate_candidate_policy(
            meta, regime_mode, cfg, profile, quality_score, current_price, settings,
        )
        row_status = str(row.get("status") or "ready").lower()
        if row_status == "validated":
            positive, terminal, reason, direction_snapshot = _combine_directional_policy(
                row, policy_eligible, policy_reason, policy_data, current_price, cfg,
            )
        else:
            positive = policy_eligible
            terminal = not policy_eligible
            reason = policy_reason
            direction_snapshot = {}
        input_features["policy"] = policy_data
        input_features["direction"] = direction_snapshot
        directional_shadow = evaluate_directional_shadow(row, policy_data, current_price, cfg)
        directional_shadow["legacy"] = {
            "positive": bool(positive),
            "terminal": bool(terminal),
            "reason": reason,
        }
        directional_shadow["would_change_execution"] = bool(
            (directional_shadow.get("action") == "CONFIRM") != bool(positive)
            or bool(directional_shadow.get("terminal")) != bool(terminal)
        )
        input_features["directional_shadow"] = directional_shadow
        total = row["total_checks"] + 1
        current_data = row.get("data") or {}
        expected_revision = int(current_data.get("candidate_revision", 0) or 0)
        data_update = {
            "policy": policy_data,
            "priority": policy_data.get("priority"),
            "directional_shadow_last": directional_shadow,
            **_stage_payload(
                current_data,
                stage_kind="trigger_monitoring",
                stage_reason="TRIGGER_RECHECK",
                max_age_min=int(row.get("max_age_min") or cfg.get("max_age_min") or 0),
            ),
        }
        if direction_snapshot.get("direction"):
            data_update["direction"] = direction_snapshot["direction"]
        if positive:
            positives = row["positive_checks"] + 1
            if row_status == "validated":
                applied = _update_ready_status(
                    db_client, ready_id, positive_checks=positives, total_checks=total, data_update=data_update,
                    expected_revision=expected_revision,
                )
                if applied:
                    expected_revision += 1
                    validated.append(symbol)
                    decision = "trigger_confirmed"
                else:
                    decision = "concurrent_conflict"
            else:
                applied = _update_ready_status(
                    db_client, ready_id, positive_checks=positives, total_checks=total, data_update=data_update,
                    expected_revision=expected_revision,
                )
                if applied:
                    expected_revision += 1
                if not applied:
                    decision = "concurrent_conflict"
                else:
                    if positives >= int(row["required_positive_checks"]):
                        _update_ready_status(db_client, ready_id, status="validated", validated_at=True)
                        _update_ready_status(
                            db_client,
                            ready_id,
                            data_update=_stage_payload(
                                data_update,
                                stage_kind="risk_authorized",
                                stage_reason="TRIGGER_FIRED_VALIDATED",
                                max_age_min=int(row.get("max_age_min") or cfg.get("max_age_min") or 0),
                            ),
                        )
                        validated.append(symbol)
                    decision = "trigger_confirmed"
        elif terminal:
            data_update["last_rejection"] = {"reason": reason, "direction": direction_snapshot}
            data_update.update(
                _stage_payload(
                    data_update,
                    stage_kind="discarded",
                    stage_reason=_terminal_stage_reason(reason),
                    max_age_min=int(row.get("max_age_min") or cfg.get("max_age_min") or 0),
                )
            )
            applied = _update_ready_status(
                db_client, ready_id, total_checks=total, data_update=data_update,
                expected_revision=expected_revision,
            )
            if applied:
                expected_revision += 1
            decision = "terminal_reject" if applied else "concurrent_conflict"
        else:
            applied = _update_ready_status(
                db_client, ready_id, total_checks=total, data_update=data_update,
                expected_revision=expected_revision,
            )
            if applied:
                expected_revision += 1
            decision = "trigger_pending" if applied else "concurrent_conflict"

        input_features["stage_kind"] = data_update.get("stage_kind")
        input_features["stage_reason"] = data_update.get("stage_reason")

        _record_check(
            db_client, ready_id, None, _next_check_sequence(db_client, ready_id), "intra",
            current_price, price_source, profile, quality_score,
            gate_info.get("thresholds"), fear_greed, input_features, positive, decision, reason,
            execution_ms, writer="intra_legacy",
        )

        if terminal and decision != "concurrent_conflict":
            if _discard_ready(db_client, ready_id, reason, expected_revision=expected_revision):
                expected_revision += 1
                row["status"] = "discarded"

        _maybe_expire(db_client, row, expected_revision=expected_revision)

    return validated


def _discard_ready(
    db_client: Any,
    ready_id: int,
    reason: str,
    expected_revision: Optional[int] = None,
) -> bool:
    stage_reason = _terminal_stage_reason(reason)
    return _update_ready_status(
        db_client,
        ready_id,
        status="discarded",
        discard_reason=reason,
        discarded_at=True,
        data_update={
            "stage_kind": "discarded",
            "stage_reason": stage_reason,
            "stage_updated_at": _now().isoformat(),
        },
        expected_revision=expected_revision,
    )


def discard_ready(
    db_client: Any,
    symbol: str,
    trade_type: str,
    regime_mode: str,
    reason: str,
) -> bool:
    row = _fetch_one(
        db_client,
        "SELECT id FROM ready_candidates WHERE symbol=%s AND trade_type=%s AND regime_mode=%s AND status IN ('ready','validated')",
        (symbol, trade_type, regime_mode),
    )
    if not row:
        return False
    _discard_ready(db_client, row["id"], reason)
    return True


def get_openable_candidates(
    db_client: Any,
    regime_mode: str,
    top_symbols_qubo: Optional[List[str]] = None,
    settings: Any = None,
) -> List[Dict[str, Any]]:
    """Return the highest-priority non-expired validated candidate in the active regime."""
    if not db_client:
        return []
    try:
        rows = db_client.execute_query(
            """
            SELECT * FROM ready_candidates
            WHERE status='validated' AND regime_mode=%s
            ORDER BY ready_at ASC
            """,
            (regime_mode,),
        )
        cfg = get_staged_entry_regime_config(settings, regime_mode)
        active_rows: List[Dict[str, Any]] = []
        for row in rows or []:
            for key in ("signal_meta", "config_snapshot", "data"):
                if row.get(key) and isinstance(row[key], str):
                    try:
                        row[key] = json.loads(row[key])
                    except Exception:
                        pass
            max_age = int(row.get("max_age_min") or cfg.get("max_age_min") or 0)
            age_base = row.get("validated_at") or row.get("ready_at")
            if max_age and age_base and (_now() - age_base).total_seconds() / 60.0 >= max_age:
                _row_revision = int((row.get("data") or {}).get("candidate_revision", 0) or 0)
                if _discard_ready(db_client, row["id"], "MAX_AGE_EXPIRED", expected_revision=_row_revision):
                    _record_check(
                        db_client, row["id"], None, _next_check_sequence(db_client, row["id"]), "lifecycle",
                        None, None, None, 0.0, {}, None, {"validated_at": str(age_base), "max_age_min": max_age},
                        False, "terminal_reject", "MAX_AGE_EXPIRED", 0, writer="risk_selector",
                    )
                continue
            if _apply_stale_hold_policy(db_client, row, cfg, settings):
                continue
            direction = (row.get("data") or {}).get("direction") or {}
            required_confirmations = max(1, int(cfg.get("direction_required_confirmations", 2) or 2))
            if _direction_mode(cfg) == "enforced" and (
                str(direction.get("state") or "") != "confirmed"
                or int(direction.get("directional_confirmations", 0) or 0) < required_confirmations
            ):
                # Keep the candidate validated and let the watcher keep monitoring it.
                # Do NOT requeue to ready, which resets the clock and can cause MAX_AGE_EXPIRED.
                continue
            active_rows.append(row)

        def priority(row: Dict[str, Any]) -> Tuple[float, float, float, float, float]:
            policy = ((row.get("data") or {}).get("policy") or {})
            expected_pnl = policy.get("expected_pnl")
            quality_score = policy.get("quality_score")
            probability = policy.get("success_probability")
            try:
                expected_pnl = float(expected_pnl)
            except (TypeError, ValueError):
                expected_pnl = -float("inf")
            try:
                quality_score = float(quality_score)
            except (TypeError, ValueError):
                quality_score = -float("inf")
            try:
                probability = float(probability)
            except (TypeError, ValueError):
                probability = -float("inf")
            ready_at = row.get("ready_at")
            ready_timestamp = ready_at.timestamp() if ready_at and hasattr(ready_at, "timestamp") else float("inf")
            # s111: a candidate whose most recent real execution attempt failed
            # (`last_execution_gate.passed=False`, set by `_update_fast_path_gate`) drops
            # BEHIND every candidate that hasn't failed yet, regardless of raw EV/quality
            # score. Measured on 48h of production data: candidates that eventually got
            # discarded consumed 2,171 of 2,335 total risk-queue selections (93%) — e.g.
            # HEIUSDT/RIFUSDT held the only 2 `max_open_per_cycle` slots almost continuously
            # for 46 minutes on 2026-08-02 despite repeatedly failing to execute, starving 6
            # other genuinely executable candidates. A high EV score at signal time does not
            # mean the setup can actually be filled; a fresh execution failure is much
            # stronger evidence of non-viability than the (often stale/optimistic) EV model.
            _last_gate = (row.get("data") or {}).get("last_execution_gate") or {}
            _execution_viable = 0.0 if _last_gate.get("passed") is False else 1.0
            return (_execution_viable, expected_pnl, quality_score, probability, -ready_timestamp)

        active_rows = sorted(active_rows, key=priority, reverse=True)
        selected = active_rows[:max(1, int(cfg.get("max_open_per_cycle", 1)))]
        for rank, row in enumerate(selected, start=1):
            _risk_stage = _stage_payload(
                row.get("data") or {},
                stage_kind="risk_authorized",
                stage_reason="RISK_QUEUE_SELECTED",
                max_age_min=int(row.get("max_age_min") or cfg.get("max_age_min") or 0),
            )
            _row_revision = int((row.get("data") or {}).get("candidate_revision", 0) or 0)
            applied = _update_ready_status(
                db_client,
                row["id"],
                data_update={
                    "global_validated_rank": rank,
                    "global_validated_universe_size": len(active_rows),
                    "selection_criterion": "EV_NET_DESC_QUALITY_PROBABILITY_READY_AT",
                    **_risk_stage,
                },
                guard_status="validated",
                expected_revision=_row_revision,
            )
            if applied:
                _record_check(
                    db_client, row["id"], None, _next_check_sequence(db_client, row["id"]), "lifecycle",
                    None, None, None, 0.0, {}, None,
                    {"global_validated_rank": rank, "global_validated_universe_size": len(active_rows)},
                    True, "risk_queue_selected", "RISK_QUEUE_SELECTED", 0, writer="risk_selector",
                )
        return selected
    except Exception as exc:
        logger.warning(f"[StagedEntry] get_openable_candidates failed: {exc}")
        return []


def mark_opened(
    db_client: Any,
    ready_id: int,
    trade_id: int,
) -> None:
    _update_ready_status(
        db_client,
        ready_id,
        status="opened",
        opened_at=True,
        opened_trade_id=trade_id,
        data_update={
            "stage_kind": "opened",
            "stage_reason": "EXECUTION_CONFIRMED",
            "stage_updated_at": _now().isoformat(),
        },
    )
    _record_check(
        db_client, ready_id, None, _next_check_sequence(db_client, ready_id), "lifecycle",
        None, None, None, 0.0, {}, None, {"trade_id": trade_id},
        True, "opened", "EXECUTION_CONFIRMED", 0, writer="main_cycle",
    )


def cleanup_old(db_client: Any, retention_days: int = 7) -> None:
    if not db_client:
        return
    try:
        db_client.execute_update(
            """
            DELETE FROM ready_candidates
            WHERE status IN ('discarded','opened')
              AND updated_at < NOW() - INTERVAL '%s days'
            """,
            (retention_days,),
        )
    except Exception as exc:
        logger.warning(f"[StagedEntry] cleanup_old failed: {exc}")


def discard_ready_by_regime_change(
    db_client: Any,
    old_regime: str,
    new_regime: str,
) -> List[str]:
    """Discard all ready/validated candidates from a regime that is no longer active."""
    if not db_client or old_regime == new_regime:
        return []
    try:
        rows = db_client.execute_query(
            "SELECT id, symbol, data FROM ready_candidates WHERE regime_mode=%s AND status IN ('ready','validated')",
            (old_regime,),
        )
        discarded: List[str] = []
        for row in rows or []:
            row_data = row.get("data") or {}
            if isinstance(row_data, str):
                try:
                    row_data = json.loads(row_data)
                except Exception:
                    row_data = {}
            expected_revision = int(row_data.get("candidate_revision", 0) or 0)
            reason = f"REGIME_CHANGED {old_regime} -> {new_regime}"
            if _discard_ready(db_client, row["id"], reason, expected_revision=expected_revision):
                _record_check(
                    db_client, row["id"], None, _next_check_sequence(db_client, row["id"]), "lifecycle",
                    None, None, None, 0.0, {}, None, {"old_regime": old_regime, "new_regime": new_regime},
                    False, "terminal_reject", reason, 0, writer="regime_guard",
                )
                discarded.append(row["symbol"])
        return discarded
    except Exception as exc:
        logger.warning(f"[StagedEntry] discard_ready_by_regime_change failed: {exc}")
        return []

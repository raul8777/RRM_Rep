"""staged_entry.py — multi-cycle ready-state validation before opening positions.

Provides a persistent, auditable `ready` state between the entry-profile gate
/QUBO selection and actual trade execution.  Each re-check (intra- or inter-cycle)
is stored in `ready_candidate_checks` with the full feature snapshot so future
analysis can correlate validation decisions with realized PnL.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from config_helpers import get_staged_entry_regime_config
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


def _trade_type_for_regime(regime_mode: str) -> str:
    return "futures" if regime_mode == "FUTURES_SHORT" else "spot"


def _side_for_regime(regime_mode: str) -> str:
    return "SHORT" if regime_mode == "FUTURES_SHORT" else "LONG"


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
        lower = _number(bollinger.get("lower") if isinstance(bollinger, dict) else None)
        upper = _number(bollinger.get("upper") if isinstance(bollinger, dict) else None)
        if lower is None:
            lower = _number(meta.get("bb_lower"))
        if upper is None:
            upper = _number(meta.get("bb_upper"))
        middle = _number(bollinger.get("middle") if isinstance(bollinger, dict) else None)
        if middle is None:
            middle = _number(meta.get("bb_middle"))
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
        import pnl_service
        return pnl_service.get_predictor().predict_range_reversion(
            current_price=price,
            indicators=source,
            position_size_usdt=_position_size_for_validation(meta),
            settings=settings,
            trade_type=meta.get("trade_type") or "spot",
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
    target_price = _number(policy.get("target_price"))
    stop_price = _number(policy.get("stop_price"))
    last_price = _number(direction.get("last_observed_price")) or base_price
    min_progress_bps = max(0.0, float(cfg.get("direction_min_progress_bps", 0.0) or 0.0))
    max_adverse_bps = max(0.0, float(cfg.get("direction_max_adverse_bps", 0.0) or 0.0))
    required = max(1, int(cfg.get("direction_required_confirmations", 2) or 2))
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
        return False, True, "DIRECTION_PRICE_MISSING", snapshot
    if base_price is None or base_price <= 0 or last_price is None or last_price <= 0:
        snapshot["state_after"] = state
        return False, True, "DIRECTION_REFERENCE_MISSING", snapshot
    is_short = side == "SHORT"
    direction_sign = -1.0 if is_short else 1.0
    progress_bps = direction_sign * (price - last_price) / last_price * 10_000.0
    base_progress_bps = direction_sign * (price - base_price) / base_price * 10_000.0
    snapshot["progress_bps"] = progress_bps
    snapshot["base_progress_bps"] = base_progress_bps
    if row.get("regime_mode") == "RANGE_SCALP":
        if target_price is None or target_price <= 0:
            snapshot["state_after"] = state
            return False, True, "DIRECTION_TARGET_MISSING", snapshot
        if (not is_short and target_price <= price) or (is_short and target_price >= price):
            snapshot["state_after"] = state
            return False, True, "DIRECTION_TARGET_INVALID", snapshot
    if stop_price is not None and ((not is_short and price <= stop_price) or (is_short and price >= stop_price)):
        snapshot["state_after"] = state
        return False, True, "DIRECTION_STOP_BREACHED", snapshot
    if max_adverse_bps and base_progress_bps <= -max_adverse_bps:
        snapshot["state_after"] = state
        return False, True, "DIRECTION_DRAWDOWN_LIMIT", snapshot
    target_progress = True
    if target_price is not None:
        target_progress = abs(target_price - price) < abs(target_price - last_price)
    snapshot["target_progress"] = target_progress
    favorable = progress_bps >= min_progress_bps and target_progress
    updated = dict(direction)
    updated.update({
        "base_price": base_price,
        "side": side,
        "target_price": target_price,
        "stop_price": stop_price,
        "last_observed_price": price,
        "last_observed_at": _now().isoformat(),
    })
    if favorable:
        if state == "recovery_pending":
            if base_progress_bps < min_progress_bps:
                updated["state"] = "recovery_pending"
                snapshot["state_after"] = updated["state"]
                snapshot["direction"] = updated
                return False, False, "DIRECTION_RECOVERY_NOT_RECLAIMED", snapshot
            confirmations = 1
            updated["state"] = "recovery_confirming"
        else:
            confirmations += 1
            updated["state"] = "confirmed" if confirmations >= required else "awaiting_confirmation"
        updated["directional_confirmations"] = confirmations
        updated["last_confirmed_price"] = price
        updated["last_confirmed_at"] = _now().isoformat()
        snapshot["state_after"] = updated["state"]
        snapshot["directional_confirmations"] = confirmations
        snapshot["direction"] = updated
        if confirmations >= required:
            return True, False, "DIRECTION_CONFIRMED", snapshot
        return False, False, "DIRECTION_CONFIRMING", snapshot
    if adverse_observations == 0:
        updated["state"] = "recovery_pending"
        updated["adverse_observations"] = 1
        updated["directional_confirmations"] = 0
        snapshot["state_after"] = updated["state"]
        snapshot["direction"] = updated
        return False, False, "DIRECTION_RECOVERY_PENDING", snapshot
    snapshot["state_after"] = state
    snapshot["direction"] = updated
    return False, True, "DIRECTION_NOT_CONFIRMED", snapshot


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
) -> None:
    if not db_client:
        return
    try:
        db_client.execute_update(
            """
            INSERT INTO ready_candidate_checks (
                ready_candidate_id, cycle_id, check_sequence, check_type, checked_at,
                current_price, price_source, expected_pnl_pct, quality_score, profile_label,
                thresholds_snapshot, fear_greed, input_features, positive, decision, reason, execution_ms
            ) VALUES (
                %s, %s, %s, %s, NOW(),
                %s, %s, %s, %s, %s,
                %s::jsonb, %s, %s::jsonb, %s, %s, %s, %s
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
) -> None:
    if not db_client:
        return
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
        fields.append("data = data || %s::jsonb")
        params.append(_json(data_update))
    if not fields:
        return
    sql = "UPDATE ready_candidates SET " + ", ".join(fields) + ", updated_at = NOW() WHERE id = %s"
    params.append(int(ready_id))
    try:
        db_client.execute_update(sql, tuple(params))
    except Exception as exc:
        logger.warning(f"[StagedEntry] _update_ready_status failed for ready_id={ready_id}: {exc}")


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
        meta["trade_type"] = trade_type
        meta["expected_side"] = _side_for_regime(regime_mode)
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
        eligible, policy_reason, policy_data = _evaluate_candidate_policy(
            meta, regime_mode, cfg, meta.get("peak_quality_profile"),
            float(meta.get("peak_quality_score", 0.0) or 0.0), signal_price, settings,
        )
        if not eligible:
            continue
        position_size = _position_size_for_validation(meta)
        required = int(cfg.get("required_positive_checks", 2))
        checks_per_cycle = int(cfg.get("checks_per_cycle", 1))
        max_cycles = int(cfg.get("max_cycles", 2))
        max_age = int(cfg.get("max_age_min", 25))

        _initial_direction_mode = _direction_mode(cfg)
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
                    _json(meta), _json(cfg), _json({
                        "cycles_active": 1,
                        "policy": policy_data,
                        "priority": policy_data.get("priority"),
                        "direction": {
                            "state": "awaiting_confirmation",
                            "base_price": float(signal_price) if signal_price is not None else None,
                            "last_observed_price": float(signal_price) if signal_price is not None else None,
                            "side": meta.get("expected_side") or _side_for_regime(regime_mode),
                            "target_price": policy_data.get("target_price"),
                            "stop_price": policy_data.get("stop_price"),
                            "directional_confirmations": 0,
                            "adverse_observations": 0,
                        },
                    }),
                ),
            )
            ready_id = rows[0]["id"]
        except Exception as exc:
            logger.warning(f"[StagedEntry] failed to insert ready candidate {symbol}: {exc}")
            continue

        _record_check(
            db_client, ready_id, cycle_id, 0, "inter", signal_price, cfg.get("price_source"),
            meta.get("peak_quality_profile"), float(meta.get("peak_quality_score", 0.0) or 0.0),
            (meta.get("entry_profile_gate_info") or {}).get("thresholds"),
            fear_greed, {**_input_features_for_check(meta, signal_price, signal_price), "policy": policy_data,
                         "direction": {"state_before": "setup", "state_after": "awaiting_confirmation"}},
            _initial_positive, "setup" if not _initial_positive else "positive", policy_reason, 0,
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

    trade_type = _trade_type_for_regime(regime_mode)
    fear_greed = _get_fear_greed_value(news_client)
    top_by_symbol = {_symbol_from_candidate(c): c for c in top_signals if _symbol_from_candidate(c)}

    readies = load_ready_candidates(db_client, regime_mode=regime_mode)
    if not readies:
        return result

    for row in readies:
        ready_id = row["id"]
        symbol = row["symbol"]
        start = time.monotonic()

        meta = _build_meta_for_symbol(symbol, signal_meta, predictions, top_by_symbol.get(symbol))
        meta.update(row.get("signal_meta") or {})
        meta["trade_type"] = trade_type
        meta["expected_side"] = _side_for_regime(regime_mode)

        # Advance cycle counter for every inter-cycle evaluation.
        _data = row.get("data") or {}
        _cycles_active = int(_data.get("cycles_active", 1)) + 1
        if isinstance(_data, dict):
            _data["cycles_active"] = _cycles_active

        # If the symbol is in the new top signals, use the already-computed pipeline.
        in_top = symbol in top_by_symbol
        current_price: Optional[float] = None
        if in_top:
            current_price = meta.get("current_price") or meta.get("price")
        else:
            # Re-run predictor for candidates that the pipeline did not select this cycle.
            current_price = _get_price(client, symbol, cfg.get("price_source", "avg"))
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
        positive, terminal, reason, direction_snapshot = _combine_directional_policy(
            row, policy_eligible, policy_reason, policy_data, current_price, cfg,
        )
        input_features["policy"] = policy_data
        input_features["direction"] = direction_snapshot
        total = row["total_checks"] + 1
        data_update = {
            "cycles_active": _cycles_active,
            "policy": policy_data,
            "priority": policy_data.get("priority"),
        }
        if direction_snapshot.get("direction"):
            data_update["direction"] = direction_snapshot["direction"]
        if positive:
            positives = row["positive_checks"] + 1
            if _direction_mode(cfg) == "enforced":
                positives = max(positives, int(row["required_positive_checks"]))
            decision = "positive"
            _update_ready_status(
                db_client, ready_id, positive_checks=positives, total_checks=total, data_update=data_update,
            )
            if positives >= int(row["required_positive_checks"]):
                _update_ready_status(db_client, ready_id, status="validated", validated_at=True)
                row["status"] = "validated"
                result["validated"].append(symbol)
            else:
                result["kept"].append(symbol)
        elif terminal:
            decision = "negative"
            data_update["last_rejection"] = {"reason": reason, "direction": direction_snapshot}
            _update_ready_status(db_client, ready_id, total_checks=total, data_update=data_update)
            _discard_ready(db_client, ready_id, reason)
            row["status"] = "discarded"
            result["discarded"].append(symbol)
        else:
            decision = "pending"
            _update_ready_status(db_client, ready_id, total_checks=total, data_update=data_update)
            result["kept"].append(symbol)

        _record_check(
            db_client, ready_id, cycle_id, _next_check_sequence(db_client, ready_id), "inter",
            current_price, cfg.get("price_source"), profile, quality_score,
            gate_info.get("thresholds"), fear_greed, input_features, positive, decision, reason,
            execution_ms,
        )

        if _maybe_expire(db_client, row):
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


def _maybe_expire(db_client: Any, row: Dict[str, Any]) -> bool:
    """Discard a ready candidate if it exceeds max_age or max_cycles.

    Validated candidates are kept so they can still be opened this cycle.
    Returns True if the row was discarded.
    """
    ready_id = row["id"]
    status = row["status"]
    if status != "ready":
        return False
    max_age = int(row.get("max_age_min") or 0)
    if max_age and row.get("ready_at"):
        age_min = (_now() - row["ready_at"]).total_seconds() / 60.0
        if age_min >= max_age:
            _discard_ready(db_client, ready_id, "MAX_AGE_EXPIRED")
            row["status"] = "discarded"
            row["discard_reason"] = "MAX_AGE_EXPIRED"
            return True
    data = row.get("data") or {}
    cycles_active = int(data.get("cycles_active", 1))
    max_cycles = int(row.get("max_cycles") or 0)
    if max_cycles and cycles_active >= max_cycles:
        _discard_ready(db_client, ready_id, "MAX_CYCLES_REACHED")
        row["status"] = "discarded"
        row["discard_reason"] = "MAX_CYCLES_REACHED"
        return True
    return False


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

    trade_type = _trade_type_for_regime(regime_mode)
    fear_greed = _get_fear_greed_value(news_client)
    readies = load_ready_candidates(db_client, regime_mode=regime_mode, status=("ready",))
    if not readies:
        return validated

    price_source = cfg.get("price_source", "avg")
    for row in readies:
        ready_id = row["id"]
        symbol = row["symbol"]
        start = time.monotonic()

        meta = row.get("signal_meta") or {}
        meta["trade_type"] = trade_type
        meta["expected_side"] = _side_for_regime(regime_mode)

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
        positive, terminal, reason, direction_snapshot = _combine_directional_policy(
            row, policy_eligible, policy_reason, policy_data, current_price, cfg,
        )
        input_features["policy"] = policy_data
        input_features["direction"] = direction_snapshot
        total = row["total_checks"] + 1
        data_update = {"policy": policy_data, "priority": policy_data.get("priority")}
        if direction_snapshot.get("direction"):
            data_update["direction"] = direction_snapshot["direction"]
        if positive:
            positives = row["positive_checks"] + 1
            if _direction_mode(cfg) == "enforced":
                positives = max(positives, int(row["required_positive_checks"]))
            _update_ready_status(
                db_client, ready_id, positive_checks=positives, total_checks=total, data_update=data_update,
            )
            if positives >= int(row["required_positive_checks"]):
                _update_ready_status(db_client, ready_id, status="validated", validated_at=True)
                validated.append(symbol)
            decision = "positive"
        elif terminal:
            data_update["last_rejection"] = {"reason": reason, "direction": direction_snapshot}
            _update_ready_status(db_client, ready_id, total_checks=total, data_update=data_update)
            decision = "negative"
        else:
            _update_ready_status(db_client, ready_id, total_checks=total, data_update=data_update)
            decision = "pending"

        _record_check(
            db_client, ready_id, None, _next_check_sequence(db_client, ready_id), "intra",
            current_price, price_source, profile, quality_score,
            gate_info.get("thresholds"), fear_greed, input_features, positive, decision, reason,
            execution_ms,
        )

        if terminal:
            _discard_ready(db_client, ready_id, reason)

        _maybe_expire(db_client, row)

    return validated


def _discard_ready(db_client: Any, ready_id: int, reason: str) -> None:
    _update_ready_status(
        db_client, ready_id, status="discarded", discard_reason=reason, discarded_at=True
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
    trade_type = _trade_type_for_regime(regime_mode)
    try:
        rows = db_client.execute_query(
            """
            SELECT * FROM ready_candidates
            WHERE status='validated' AND regime_mode=%s AND trade_type=%s
            ORDER BY ready_at ASC
            """,
            (regime_mode, trade_type),
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
            ready_at = row.get("ready_at")
            if max_age and ready_at and (_now() - ready_at).total_seconds() / 60.0 >= max_age:
                _discard_ready(db_client, row["id"], "MAX_AGE_EXPIRED")
                continue
            direction = (row.get("data") or {}).get("direction") or {}
            required_confirmations = max(2, int(cfg.get("direction_required_confirmations", 2) or 2))
            if _direction_mode(cfg) == "enforced" and (
                str(direction.get("state") or "") != "confirmed"
                or int(direction.get("directional_confirmations", 0) or 0) < required_confirmations
            ):
                _update_ready_status(
                    db_client,
                    row["id"],
                    status="ready",
                    positive_checks=0,
                    data_update={
                        "direction_requeue": {
                            "reason": "DIRECTION_NOT_CONFIRMED",
                            "required_confirmations": required_confirmations,
                            "recorded_confirmations": int(direction.get("directional_confirmations", 0) or 0),
                            "requeued_at": _now().isoformat(),
                        }
                    },
                )
                continue
            active_rows.append(row)

        def priority(row: Dict[str, Any]) -> Tuple[float, float, float, float]:
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
            return (expected_pnl, quality_score, probability, -ready_timestamp)

        active_rows = sorted(active_rows, key=priority, reverse=True)
        selected = active_rows[:max(1, int(cfg.get("max_open_per_cycle", 1)))]
        for rank, row in enumerate(selected, start=1):
            _update_ready_status(
                db_client,
                row["id"],
                data_update={
                    "global_validated_rank": rank,
                    "global_validated_universe_size": len(active_rows),
                    "selection_criterion": "EV_NET_DESC_QUALITY_PROBABILITY_READY_AT",
                },
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
    _update_ready_status(db_client, ready_id, status="opened", opened_at=True, opened_trade_id=trade_id)


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
            "SELECT id, symbol FROM ready_candidates WHERE regime_mode=%s AND status IN ('ready','validated')",
            (old_regime,),
        )
        discarded: List[str] = []
        for row in rows or []:
            _discard_ready(db_client, row["id"], f"REGIME_CHANGED {old_regime} -> {new_regime}")
            discarded.append(row["symbol"])
        return discarded
    except Exception as exc:
        logger.warning(f"[StagedEntry] discard_ready_by_regime_change failed: {exc}")
        return []

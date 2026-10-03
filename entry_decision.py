from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class EntryDecision:
    eligible: bool
    reason: str
    policy_version: str
    regime: str
    profile: Optional[str]
    quality_score: float
    model: Optional[str]
    expected_pnl: Optional[float]
    expected_pnl_pct: Optional[float]
    success_probability: Optional[float]
    target_price: Optional[float]
    stop_price: Optional[float]
    priority: Tuple[float, float, float]

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["priority"] = list(self.priority)
        return data


def _number(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _mode(settings: Any, regime: str) -> str:
    policies = getattr(
        settings,
        "entry_decision_regime_mode",
        {"SPOT_BULL": "enforced", "RANGE_SCALP": "enforced", "FUTURES_SHORT": "enforced", "PRESERVATION": "enforced"},
    ) or {}
    return str(policies.get(regime, "enforced")).strip().lower()


def _decision(
    eligible: bool,
    reason: str,
    settings: Any,
    regime: str,
    profile: Optional[str],
    quality_score: float,
    model: Optional[str],
    expected_pnl: Optional[float],
    expected_pnl_pct: Optional[float],
    probability: Optional[float],
    target_price: Optional[float],
    stop_price: Optional[float],
) -> EntryDecision:
    if regime == "RANGE_SCALP":
        priority = (quality_score, probability if probability is not None else -1.0, expected_pnl if expected_pnl is not None else -float("inf"))
    else:
        priority = (expected_pnl if expected_pnl is not None else -float("inf"), quality_score, probability if probability is not None else -1.0)
    return EntryDecision(
        eligible=eligible,
        reason=reason,
        policy_version=str(getattr(settings, "entry_decision_policy_version", "strict_v1")),
        regime=regime,
        profile=profile,
        quality_score=quality_score,
        model=model,
        expected_pnl=expected_pnl,
        expected_pnl_pct=expected_pnl_pct,
        success_probability=probability,
        target_price=target_price,
        stop_price=stop_price,
        priority=priority,
    )


def evaluate(
    meta: Dict[str, Any],
    regime: str,
    profile: Optional[str],
    quality_score: float,
    settings: Any,
    current_price: Optional[float] = None,
) -> EntryDecision:
    regime = str(regime or "SPOT_BULL").upper()
    enabled = bool(getattr(settings, "entry_decision_enabled", True))
    prediction = meta.get("entry_decision_prediction") or meta.get("pnl_prediction") or {}
    if not isinstance(prediction, dict):
        prediction = {}
    expected_pnl = _number(prediction.get("expected_pnl", meta.get("expected_pnl")))
    expected_pnl_pct = _number(prediction.get("expected_pnl_percentage", meta.get("expected_pnl_percentage", meta.get("expected_pnl_pct"))))
    probability = _number(prediction.get("success_probability", meta.get("success_probability")))
    model = prediction.get("model")
    target_price = _number(prediction.get("target_price"))
    stop_price = _number(prediction.get("stop_price"))
    # If the generic predictor produced a price_range but no explicit target/stop,
    # use the range extremes so downstream staged-entry direction confirmation
    # has a reference even when the range-reversion model is invalid.
    if target_price is None or stop_price is None:
        price_range = prediction.get("price_range") or {}
        if isinstance(price_range, dict):
            _min = _number(price_range.get("min"))
            _max = _number(price_range.get("max"))
            if _min is not None and _max is not None:
                _side = str(meta.get("expected_side") or "LONG").strip().upper()
                if _side == "SHORT":
                    target_price = target_price or _min
                    stop_price = stop_price or _max
                else:
                    target_price = target_price or _max
                    stop_price = stop_price or _min
    if not enabled:
        return _decision(True, "ENTRY_DECISION_DISABLED", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)
    if _mode(settings, regime) != "enforced":
        return _decision(False, "REGIME_NOT_ENABLED", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)
    if profile not in ("A", "B", "C"):
        return _decision(False, "QUALITY_BELOW_C", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)
    if meta.get("degenerate_output") or prediction.get("degenerate_output"):
        return _decision(False, "PREDICTOR_INVALID", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)
    if regime == "RANGE_SCALP":
        arbitration = meta.get("directional_arbitration") if isinstance(meta.get("directional_arbitration"), dict) else {}
        thesis_type = str(
            meta.get("thesis_type")
            or arbitration.get("selected_thesis_type")
            or prediction.get("thesis_type")
            or ""
        ).strip().upper()
        reversal_theses = {"LONG_SUPPORT_BOUNCE", "SHORT_RESISTANCE_REJECTION"}
        continuation_theses = {
            "LONG_RESISTANCE_BREAKOUT", "SHORT_SUPPORT_BREAKDOWN",
            "LONG_DIRECTIONAL_EDGE", "SHORT_DIRECTIONAL_EDGE",
        }
        range_prediction = meta.get("range_reversion_prediction")
        range_mode = str(getattr(settings, "range_reversion_predictor_mode", "enforced")).lower()
        if thesis_type in continuation_theses:
            reference_price = _number(current_price if current_price is not None else prediction.get("current_price"))
            side = str(meta.get("expected_side") or prediction.get("trade_side") or "LONG").strip().upper()
            if reference_price is None or target_price is None or stop_price is None:
                return _decision(False, "CONTINUATION_TARGET_STOP_MISSING", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)
            target_valid = target_price > reference_price if side == "LONG" else target_price < reference_price
            stop_valid = stop_price < reference_price if side == "LONG" else stop_price > reference_price
            if not target_valid or not stop_valid:
                return _decision(False, "CONTINUATION_TARGET_STOP_INVALID", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)
            model = prediction.get("model") or "directional_continuation"
        elif isinstance(range_prediction, dict) and range_prediction.get("valid"):
            expected_pnl = _number(range_prediction.get("expected_pnl"))
            expected_pnl_pct = _number(range_prediction.get("expected_pnl_percentage"))
            probability = _number(range_prediction.get("success_probability"))
            model = range_prediction.get("model")
            target_price = _number(range_prediction.get("target_price"))
            stop_price = _number(range_prediction.get("stop_price"))
        elif thesis_type in reversal_theses:
            if range_mode in ("blocking", "enforced"):
                return _decision(False, "RANGE_MODEL_INVALID", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)
        elif not thesis_type:
            model = prediction.get("model") or "pre_arbitration_pending"
        else:
            return _decision(False, "UNKNOWN_RANGE_THESIS", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)
    if expected_pnl is None or probability is None:
        return _decision(False, "MISSING_PREDICTION", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)
    min_ev = float(
        (
            getattr(
                settings,
                "entry_decision_min_expected_pnl",
                {"SPOT_BULL": 0.0, "RANGE_SCALP": 0.0, "FUTURES_SHORT": 0.0, "PRESERVATION": 0.0},
            )
            or {}
        ).get(regime, 0.0)
    )
    min_probability = float(
        (
            getattr(
                settings,
                "entry_decision_min_probability",
                {"SPOT_BULL": 0.50, "RANGE_SCALP": 0.50, "FUTURES_SHORT": 0.50, "PRESERVATION": 0.50},
            )
            or {}
        ).get(regime, 0.0)
    )
    if expected_pnl <= min_ev:
        return _decision(False, "EV_NON_POSITIVE", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)
    if probability < min_probability:
        return _decision(False, "PROBABILITY_BELOW_THRESHOLD", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)
    return _decision(True, "ENTRY_DECISION_ACCEPTED", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)

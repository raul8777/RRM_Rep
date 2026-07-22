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
    if not enabled:
        return _decision(True, "ENTRY_DECISION_DISABLED", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)
    if _mode(settings, regime) != "enforced":
        return _decision(False, "REGIME_NOT_ENABLED", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)
    if profile not in ("A", "B", "C"):
        return _decision(False, "QUALITY_BELOW_C", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)
    if meta.get("degenerate_output") or prediction.get("degenerate_output"):
        return _decision(False, "PREDICTOR_INVALID", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)
    if regime == "RANGE_SCALP":
        range_prediction = meta.get("range_reversion_prediction")
        range_mode = str(getattr(settings, "range_reversion_predictor_mode", "shadow")).lower()
        if isinstance(range_prediction, dict) and range_prediction.get("valid"):
            expected_pnl = _number(range_prediction.get("expected_pnl"))
            expected_pnl_pct = _number(range_prediction.get("expected_pnl_percentage"))
            probability = _number(range_prediction.get("success_probability"))
            model = range_prediction.get("model")
            target_price = _number(range_prediction.get("target_price"))
            stop_price = _number(range_prediction.get("stop_price"))
        elif range_mode in ("blocking", "enforced"):
            return _decision(False, "RANGE_MODEL_INVALID", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)
        else:
            # shadow mode: keep the generic long predictor already resolved above.
            # The reason tracks that the range-reversion model was skipped for observability.
            range_prediction = {"valid": False, "reason": "shadow_skipped"}
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
                {"SPOT_BULL": 0.6, "RANGE_SCALP": 0.6, "FUTURES_SHORT": 0.6, "PRESERVATION": 0.6},
            )
            or {}
        ).get(regime, 0.0)
    )
    if expected_pnl <= min_ev:
        return _decision(False, "EV_NON_POSITIVE", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)
    if probability < min_probability:
        return _decision(False, "PROBABILITY_BELOW_THRESHOLD", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)
    return _decision(True, "ENTRY_DECISION_ACCEPTED", settings, regime, profile, quality_score, model, expected_pnl, expected_pnl_pct, probability, target_price, stop_price)
